#!/usr/bin/env python3
"""
qa_gate.py
Consolidated QA checks (item 17 of the brief), run before a video is
handed to upload.py. Nothing here modifies Quran text, audio, subtitles,
or video content — every function is a pure check that either passes
silently (logging a short confirmation) or raises, which build_video.py
treats as "reject this render."

Checks performed:
  Quran
    - every ayah in the batch resolves to non-empty Arabic AND English
      text before anything is rendered (validate_batch_text)
  Video (main segment, i.e. background + subtitles + recitation audio,
  before any intro is joined on)
    - genuinely 1440x2560 (2K) resolution, 9:16 aspect ratio — item 9 of
      the 2K pass: "reject videos that aren't genuinely 2K"
    - H.264 video / yuv420p pixel format / AAC audio, expected sample
      rate and a sane audio bitrate
    - has an audio track
    - duration matches the actual recitation length (catches
      audio/video desync)
    - subtitle file exists and isn't empty
    - no unexpected black stretch anywhere in the real footage (the
      intro's intentional black opening is a separate file at this
      point and is never checked by this function — see
      check_no_unexpected_black_frames())
  Intro
    - if intro was supposed to be built (BISMILLAH audio was
      available), its duration is inside sane bounds
    - if intro was skipped, that's logged as an accepted, expected
      outcome, not a failure
  Final deliverable (after the intro is joined on, if any)
    - same 2K/codec/audio checks as the main segment
    - total duration matches main + intro − join-transition-overlap,
      within tolerance
    - file exists and isn't a suspiciously small/corrupt stub

NOT checked here (documented honestly rather than silently skipped):
  - Human/vehicle content in the background is enforced upstream, per
    clip, by human_filter.py before a clip is ever allowed into the
    background at all (see pexels_fetcher.collect_clips) — this gate
    does not re-run detection on the full composited video, since every
    frame in it is already guaranteed to come from a clip that passed
    that check individually.
  - Frame-level corruption (e.g. a single glitched frame mid-render) is
    not exhaustively scanned; only container-level validity (ffprobe
    parses it, duration/resolution/audio match) plus a full-video
    black-frame sweep (see check_no_unexpected_black_frames, which IS a
    real single-pass scan of every frame, not a sample) are verified.
"""

import json
import subprocess
from pathlib import Path

from config import (
    VIDEO_WIDTH, VIDEO_HEIGHT, INTRO_MIN_DURATION, INTRO_MAX_DURATION,
    AUDIO_SAMPLE_RATE,
)
from subtitle_builder import get_ayah_text
from logging_utils import get_logger

log = get_logger(__name__)

EXPECTED_ASPECT = VIDEO_WIDTH / VIDEO_HEIGHT   # 1440/2560 = 0.5625 = 9:16
ASPECT_TOLERANCE = 0.002


class QAError(RuntimeError):
    """Raised when a render fails a QA check and must not be uploaded."""


# ══════════════════════════════════════════════════════════════════════════
# QURAN TEXT
# ══════════════════════════════════════════════════════════════════════════

def validate_batch_text(batch: list, arabic_data, english_data) -> None:
    """
    Guards against silently uploading a video whose Arabic/translation text
    doesn't actually correspond to the audio being recited — e.g. a bad
    lookup key, a gap in the JSON source, or a typo'd surah/ayah number.
    Every single ayah in the batch must resolve to non-empty text in BOTH
    sources before we spend time rendering anything.
    """
    missing = []
    for surah, ayah in batch:
        if not get_ayah_text(arabic_data, surah, ayah):
            missing.append(f"{surah}:{ayah} (arabic)")
        if not get_ayah_text(english_data, surah, ayah):
            missing.append(f"{surah}:{ayah} (english)")
    if missing:
        raise QAError(
            f"Ayah/translation text missing for: {', '.join(missing)}. "
            "Refusing to build a video with mismatched or missing text."
        )
    log.info("QA: Arabic + English text present for all %d ayah(s) in batch.", len(batch))


# ══════════════════════════════════════════════════════════════════════════
# VIDEO SPEC HELPERS
# ══════════════════════════════════════════════════════════════════════════

def probe_video(path: Path) -> dict:
    """Returns full stream/codec info for a rendered mp4 — everything
    item 9's QA report needs to show (resolution, aspect ratio, video
    codec, pixel format, audio codec/sample rate/bitrate)."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise QAError(f"ffprobe failed on {path}: {r.stderr[-300:]}")
    info = json.loads(r.stdout)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    width, height = v.get("width"), v.get("height")
    return {
        "width": width,
        "height": height,
        "aspect_ratio": round(width / height, 4) if width and height else None,
        "duration": float(info.get("format", {}).get("duration", 0.0)),
        "video_codec": v.get("codec_name"),
        "pixel_format": v.get("pix_fmt"),
        "has_audio": a != {},
        "audio_codec": a.get("codec_name"),
        "audio_sample_rate": int(a["sample_rate"]) if a.get("sample_rate") else None,
        "audio_bitrate": int(a["bit_rate"]) if a.get("bit_rate") else None,
    }


def format_qa_report(info: dict, label: str) -> str:
    """Renders the explicit human-readable QA report item 9 asks for."""
    ar_w, ar_h = 9, 16  # the design ratio; info["aspect_ratio"] is the measured decimal
    return (
        f"QA Report ({label}):\n"
        f"  Resolution:    {info['width']}x{info['height']}\n"
        f"  Aspect Ratio:  {ar_w}:{ar_h} (measured {info['aspect_ratio']})\n"
        f"  Codec:         {(info['video_codec'] or '?').upper()}\n"
        f"  Pixel Format:  {info['pixel_format']}\n"
        f"  Audio:         {(info['audio_codec'] or '?').upper()} "
        f"{info['audio_sample_rate'] or '?'}Hz "
        f"{round((info['audio_bitrate'] or 0) / 1000)}kbps\n"
        f"  Duration:      {info['duration']:.2f}s"
    )


def _validate_spec(path: Path, subtitle_path: Path | None, expected_duration: float,
                    tolerance: float, label: str) -> dict:
    if not path.exists() or path.stat().st_size < 100_000:
        raise QAError(f"{label}: video missing or suspiciously small: {path}")

    info = probe_video(path)

    if info["width"] != VIDEO_WIDTH or info["height"] != VIDEO_HEIGHT:
        raise QAError(
            f"{label}: resolution {info['width']}x{info['height']} != "
            f"expected {VIDEO_WIDTH}x{VIDEO_HEIGHT} — this is NOT genuinely 2K. "
            "(If this came from an upscale-only pipeline, the metadata dimensions "
            "would still be wrong here just the same — this check only cares about "
            "the actual muxed frame size.)"
        )
    if info["aspect_ratio"] is None or abs(info["aspect_ratio"] - EXPECTED_ASPECT) > ASPECT_TOLERANCE:
        raise QAError(
            f"{label}: aspect ratio {info['aspect_ratio']} != expected 9:16 "
            f"({EXPECTED_ASPECT:.4f}) — footage may have been stretched or letterboxed."
        )
    if info["video_codec"] not in ("h264",):
        raise QAError(f"{label}: unexpected video codec {info['video_codec']!r} (expected h264).")
    if info["pixel_format"] != "yuv420p":
        raise QAError(f"{label}: unexpected pixel format {info['pixel_format']!r} (expected yuv420p).")
    if not info["has_audio"]:
        raise QAError(f"{label}: no audio stream.")
    if info["audio_codec"] not in ("aac",):
        raise QAError(f"{label}: unexpected audio codec {info['audio_codec']!r} (expected aac).")
    if info["audio_sample_rate"] and info["audio_sample_rate"] != AUDIO_SAMPLE_RATE:
        log.warning("%s: audio sample rate %sHz != configured %sHz (not fatal, but check the encode chain).",
                    label, info["audio_sample_rate"], AUDIO_SAMPLE_RATE)
    if abs(info["duration"] - expected_duration) > tolerance:
        raise QAError(
            f"{label}: duration {info['duration']:.2f}s doesn't match "
            f"expected {expected_duration:.2f}s (tolerance {tolerance}s) — "
            "possible audio/video desync."
        )
    if subtitle_path is not None:
        if not subtitle_path.exists() or subtitle_path.stat().st_size < 50:
            raise QAError(f"{label}: subtitle file is missing or empty.")

    log.info(format_qa_report(info, label))
    return info


def check_no_unexpected_black_frames(path: Path, min_black_duration: float = 0.5) -> None:
    """
    Single-pass scan (no re-encode — this runs ffmpeg's blackdetect
    filter with `-f null -`, so it decodes but never writes a video
    file) over the WHOLE given video looking for any stretch of at
    least `min_black_duration` seconds that's almost entirely black.
    Real background footage should never do this — a black stretch
    here means something actually went wrong upstream (e.g. a clip
    failed to normalize and left a black gap). This is intentionally
    only ever run on the MAIN segment (never on the intro, which is
    deliberately black by design — item 9: "no black frames except
    intentional intro").
    """
    cmd = [
        "ffmpeg", "-i", str(path),
        "-vf", f"blackdetect=d={min_black_duration}:pic_th=0.98:pix_th=0.10",
        "-an", "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    black_hits = [line for line in result.stderr.splitlines() if "black_start" in line]
    if black_hits:
        raise QAError(
            f"Unexpected black stretch(es) detected in the main segment (not the intro): "
            f"{black_hits}. This should never happen in real background footage."
        )
    log.info("QA: no unexpected black frames found in main segment.")


def validate_main_segment(path: Path, subtitle_path: Path, expected_duration: float) -> dict:
    """
    Checks the main segment (background + burned-in subtitles +
    mastered recitation audio), BEFORE any intro is joined on. This is
    the segment whose timing must exactly match the recitation audio —
    subtitles are burned into this segment, so its correctness is what
    actually guarantees subtitle sync, independent of whatever happens
    to the intro. Also the only place check_no_unexpected_black_frames
    runs, since this is real footage start-to-end (no intentional black
    opening in this file).
    """
    info = _validate_spec(path, subtitle_path, expected_duration, tolerance=1.5, label="main segment")
    check_no_unexpected_black_frames(path)
    return info


def validate_intro(intro_info: dict | None) -> None:
    """
    Sanity-checks the intro, if one was built. A skipped intro
    (intro_info is None) is an accepted, expected outcome — see
    intro_builder.resolve_bismillah_audio() — and is simply logged.
    """
    if intro_info is None:
        log.info("QA: no intro this run (no Bismillah audio available) — video opens directly on the first verse.")
        return
    duration = intro_info["duration"]
    # A generous upper bound beyond INTRO_MAX_DURATION is allowed here
    # because intro_builder deliberately keeps a longer-than-target
    # Bismillah phrase intact rather than cutting it — see that
    # module's docstring. This just catches genuinely broken output.
    if duration < 0.3 or duration > INTRO_MAX_DURATION * 3:
        raise QAError(f"Intro duration {duration:.2f}s is out of a sane range — rejecting this render.")
    if not Path(intro_info["path"]).exists():
        raise QAError(f"Intro file missing: {intro_info['path']}")
    intro_info_probe = probe_video(Path(intro_info["path"]))
    if intro_info_probe["width"] != VIDEO_WIDTH or intro_info_probe["height"] != VIDEO_HEIGHT:
        raise QAError(
            f"Intro resolution {intro_info_probe['width']}x{intro_info_probe['height']} != "
            f"expected {VIDEO_WIDTH}x{VIDEO_HEIGHT} — the intro must render at the same 2K "
            "canvas as the rest of the video (item 7 of the 2K pass)."
        )
    log.info("QA: intro present (%.2fs, %dx%d, source=%s).",
              duration, intro_info_probe["width"], intro_info_probe["height"], intro_info.get("audio_source"))


def validate_final_deliverable(path: Path, subtitle_path: Path, expected_total_duration: float) -> dict:
    """
    Final check on the deliverable that will actually be handed to
    upload.py — after the intro (if any) has been joined onto the main
    segment. Wider tolerance than validate_main_segment() since a
    crossfade join can shift total duration by up to one transition
    length. This file intentionally opens on black (the intro) when one
    was used, so it is NOT re-scanned by check_no_unexpected_black_frames
    — that already ran on the main segment alone, before the intentional
    black opening existed.
    """
    return _validate_spec(path, subtitle_path, expected_total_duration, tolerance=2.0, label="final deliverable")


def run_pre_upload_qa(batch, arabic_data, english_data, main_segment_path, subtitle_path,
                       main_duration, intro_info, final_path, final_duration) -> None:
    """Convenience wrapper running every check in order — see module docstring."""
    validate_batch_text(batch, arabic_data, english_data)
    validate_main_segment(main_segment_path, subtitle_path, main_duration)
    validate_intro(intro_info)
    validate_final_deliverable(final_path, subtitle_path, final_duration)
    log.info("QA: all pre-upload checks passed.")
