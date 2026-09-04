#!/usr/bin/env python3
"""
surah_renderer.py
Composites the final long-form video:
  1. background.mp4 (video-only, landscape) + mastered audio + burned-in
     ASS subtitles -> main_body.mp4 (single encode pass — subtitles are
     burned in during the SAME pass that adds audio, not a second
     re-encode on top of an already-encoded body, per spec section 3's
     "avoid repeated encode/decode cycles").
  2. intro.mp4 + main_body.mp4 + outro.mp4 -> final output, joined with
     the ffmpeg `concat` filter (not the concat demuxer) so small stream
     differences between the title-card segments and the main body don't
     require the segments to be byte-identical in encode settings.
"""

import subprocess
from pathlib import Path

from longform_config import (
    LONGFORM_VIDEO_WIDTH, LONGFORM_VIDEO_HEIGHT, LONGFORM_VIDEO_FPS,
    LONGFORM_VIDEO_CRF, LONGFORM_VIDEO_PRESET, LONGFORM_AUDIO_BITRATE, LONGFORM_AUDIO_SAMPLE_RATE,
)
from surah_validator import quick_media_check
from logging_utils import get_logger

log = get_logger(__name__)


def _probe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    return float(r.stdout.strip())


def render_main_body(background_video: Path, mastered_audio: Path, subtitles_ass: Path,
                      out_path: Path) -> None:
    """Single ffmpeg pass: mux background video + mastered audio, burn in
    subtitles, encode once at the configured long-form resolution target
    (LONGFORM_VIDEO_WIDTH/HEIGHT — 1080p by default, not necessarily 4K)."""
    vf = f"ass={subtitles_ass}"
    cmd = [
        "ffmpeg", "-y",
        "-i", str(background_video),
        "-i", str(mastered_audio),
        "-vf", vf,
        "-map", "0:v:0", "-map", "1:a:0",
        "-r", str(LONGFORM_VIDEO_FPS),
        "-c:v", "libx264", "-profile:v", "high", "-preset", LONGFORM_VIDEO_PRESET,
        "-crf", str(LONGFORM_VIDEO_CRF), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", LONGFORM_AUDIO_BITRATE, "-ar", str(LONGFORM_AUDIO_SAMPLE_RATE),
        "-shortest",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Main body render failed: {result.stderr[-800:]}")
    log.info("Main body rendered -> %s", out_path.name)


def _concat_list_file(segments: list, list_path: Path) -> None:
    with open(list_path, "w", encoding="utf-8") as f:
        for s in segments:
            # ffmpeg's concat demuxer requires escaped single quotes in paths.
            f.write(f"file '{str(s.resolve()).replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'\n")


def _try_stream_copy_join(segments: list, out_path: Path) -> bool:
    """
    Fast path: joins already-compatible segments (same codec/profile/
    pix_fmt/resolution/fps, same audio codec/samplerate/channels — see
    surah_intro_outro.py and render_main_body, which deliberately encode
    to matching parameters for exactly this) via the concat DEMUXER with
    `-c copy` — no re-encoding at all, so a multi-hour main body is never
    touched a second time just to prepend/append a few seconds of intro/
    outro. Returns True on success, False if ffmpeg reports any error
    (caller falls back to the guaranteed-correct re-encode path).
    """
    list_path = out_path.parent / f"{out_path.stem}_concat_list.txt"
    _concat_list_file(segments, list_path)
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
           "-c", "copy", str(out_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    list_path.unlink(missing_ok=True)
    if result.returncode != 0:
        log.warning("Stream-copy join failed (%s) — falling back to re-encoded join.",
                    result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error")
        return False
    return True


def join_segments(segments: list, out_path: Path) -> None:
    """
    Joins [intro?, main_body, outro?] into the final file. Tries the
    ffmpeg concat DEMUXER first (`-c copy`, zero re-encoding — see
    _try_stream_copy_join) since every segment here is deliberately
    encoded to compatible parameters; this is what keeps a multi-hour
    Surah's join step fast instead of re-encoding the whole body a
    second time (spec section 4: avoid unnecessary encode/decode/encode
    cycles). The result's duration is verified against the sum of the
    inputs' durations before it's trusted — if verification fails for
    any reason (an edge-case incompatibility in some Surah's segments),
    it falls back to the `concat` FILTER (full re-encode, tolerant of
    minor stream differences) so correctness is never sacrificed for
    speed.
    """
    segments = [s for s in segments if s is not None]
    if len(segments) == 1:
        segments[0].replace(out_path)
        log.info("Only one segment (no intro/outro) -> copied straight to %s", out_path.name)
        return

    expected_total = sum(_probe_duration(s) for s in segments)

    if _try_stream_copy_join(segments, out_path):
        actual = _probe_duration(out_path)
        if abs(actual - expected_total) <= 1.0:
            log.info("Final video assembled from %d segment(s) via stream-copy (no re-encode) -> %s",
                      len(segments), out_path.name)
            return
        log.warning("Stream-copy join produced %.1fs, expected ~%.1fs — falling back to re-encoded join.",
                    actual, expected_total)
        out_path.unlink(missing_ok=True)

    inputs = []
    for s in segments:
        inputs += ["-i", str(s)]

    n = len(segments)
    filter_parts = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
    filter_complex = f"{filter_parts}concat=n={n}:v=1:a=1[outv][outa]"

    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-r", str(LONGFORM_VIDEO_FPS),
        "-c:v", "libx264", "-profile:v", "high", "-preset", LONGFORM_VIDEO_PRESET,
        "-crf", str(LONGFORM_VIDEO_CRF), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", LONGFORM_AUDIO_BITRATE, "-ar", str(LONGFORM_AUDIO_SAMPLE_RATE),
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Segment join failed: {result.stderr[-800:]}")
    log.info("Final video assembled from %d segment(s) via re-encode -> %s", n, out_path.name)


def render_final_video(background_video: Path, mastered_audio: Path, subtitles_ass: Path,
                        intro_video: Path, outro_video: Path, work_dir: Path,
                        out_path: Path, min_duration: float = 0.0, force: bool = False) -> None:
    if out_path.exists() and not force:
        if quick_media_check(out_path, min_duration=min_duration, require_video=True, require_audio=True,
                              expected_width=LONGFORM_VIDEO_WIDTH, expected_height=LONGFORM_VIDEO_HEIGHT):
            log.info("Reusing existing rendered video -> %s", out_path.name)
            return
        log.warning("Existing rendered video at %s failed validity check — rebuilding.", out_path.name)

    main_body = work_dir / "main_body.mp4"
    reuse_main_body = (
        main_body.exists() and not force and
        quick_media_check(main_body, min_duration=0.5, require_video=True, require_audio=True,
                           expected_width=LONGFORM_VIDEO_WIDTH, expected_height=LONGFORM_VIDEO_HEIGHT)
    )
    if reuse_main_body:
        log.info("Reusing existing main body -> %s", main_body.name)
    else:
        render_main_body(background_video, mastered_audio, subtitles_ass, main_body)

    segments = [intro_video if intro_video and intro_video.exists() else None,
                main_body,
                outro_video if outro_video and outro_video.exists() else None]
    join_segments(segments, out_path)
