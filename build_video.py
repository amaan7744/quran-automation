#!/usr/bin/env python3
"""
build_video.py
Orchestrates the full pipeline: fetch ayah audio -> master audio ->
build karaoke subtitles -> pick a visual mood/template and build the
matching cinematic background -> composite the main video -> build and
join a cinematic Bismillah intro -> run the QA gate -> write extended
metadata for upload.py.

SCOPE NOTE: subtitle placement/styling/karaoke direction and deep audio
mastering are owned by subtitle_builder.py and audio_processor.py
respectively — this file only calls into them and composites their
output. Anything requiring changes to *what* the subtitles look like or
*how* the recitation is mastered belongs in those modules, not here.
subtitle_builder.py is frozen for this upgrade: this file consumes its
output exactly as before and does not alter its behavior or timing.

INTRO NOTE: the main video (background + burned-in subtitles + mastered
recitation audio) is built and QA'd as its own complete, correctly-
timed segment FIRST, exactly as it always was. The cinematic intro is
built completely separately and only joined onto the front of that
already-finished segment as the very last step (see intro_builder.py).
This ordering is what guarantees the intro can never affect subtitle
timing — subtitles are already burned-in pixels by the time the intro
exists at all.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from audio_downloader import get_next_batch, download_batch, concat_audio, save_progress
from subtitle_builder import build_subtitles, get_ayah_text
from audio_processor import master_audio
from visual_engine import choose_plan, build_background_for_plan
from intro_builder import build_intro, join_intro_and_main, IntroBuildError
from performance_metadata import build_metadata, record_generation, pick_duration_bucket, compute_video_hash
import qa_gate
import config
from config import (
    VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, TARGET_SIZE_MB, INTRO_ENABLED, INTRO_JOIN_TRANSITION,
    FINAL_ENCODE_CRF, FINAL_ENCODE_PRESET, AUDIO_BITRATE, AUDIO_SAMPLE_RATE,
)
from pexels_fetcher import PexelsError
from logging_utils import get_logger

log = get_logger(__name__)

ARABIC_JSON   = Path("arabic.json")
ENGLISH_JSON  = Path("english.json")
OUTPUT_VIDEO  = Path("output_video.mp4")
METADATA_FILE = Path("video_metadata.json")

MAX_STAGE_RETRIES = 2

# ─── REEL LENGTH ──────────────────────────────────────────────────────────
# Premium Quran Shorts/Reels stay short. Rather than always rendering a
# fixed ayah count, we fetch the usual batch and then keep only a
# Quran-ordered PREFIX of it that fits this window — never skipping,
# never repeating, never splitting an ayah mid-way. The actual
# target/hard-max window is now chosen per-run by
# performance_metadata.pick_duration_bucket() (item 12: duration
# experimentation) instead of a single fixed constant; these two
# remain as the ultimate safety fallback if that selection ever fails.
DEFAULT_TARGET_MAX_DURATION = 28.0
DEFAULT_HARD_MAX_DURATION   = 31.0

# ─── VISUAL POLISH ────────────────────────────────────────────────────────
# Slow, duration-normalized Ken Burns zoom applied to the ENTIRE
# composited background during the final render, layered on top of the
# per-clip motion styles the visual engine already applied to each
# individual clip (item 8). Kept intentionally tiny — the per-clip
# motion is now doing most of the visual-variety work; this is just a
# whole-video finishing touch, not the main source of movement anymore.
ENABLE_CINEMATIC_ZOOM = True
CINEMATIC_ZOOM_MAX    = 1.02  # ~2% zoom over the full clip — smaller than before since clips already move


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def detect_hw_encoder() -> str:
    """
    Probes ffmpeg for an available hardware H.264 encoder. Falls back to
    libx264 (software, always available and highest quality) if none work.
    """
    candidates = ["h264_nvenc", "h264_qsv", "h264_videotoolbox", "h264_vaapi"]
    try:
        listed = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, timeout=15).stdout
    except (subprocess.SubprocessError, OSError):
        return "libx264"

    for enc in candidates:
        if enc in listed:
            # Confirm it actually initializes on this machine, not just compiled in.
            probe = subprocess.run(
                ["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", "color=black:s=64x64:d=0.1",
                 "-c:v", enc, "-f", "null", "-"],
                capture_output=True, text=True, timeout=15,
            )
            if probe.returncode == 0:
                log.info("Hardware encoder available: %s", enc)
                return enc
    log.info("No usable hardware encoder found — using libx264 (software).")
    return "libx264"


def fit_batch_to_duration(batch: list, audio_files: list, audio_durations: list,
                           target_max: float, hard_max: float):
    """
    Enforces the target Reel length (item 12: duration experimentation —
    target_max/hard_max are chosen per-run by
    performance_metadata.pick_duration_bucket()) by keeping only a
    Quran-ordered PREFIX of the fetched batch. Ayahs beyond the cap are
    simply left unused this run — save_progress() only advances to the
    last ayah actually included, so nothing is skipped, split, or
    repeated; the remainder is picked up on the next run.

    Always keeps at least the first ayah, even if it alone exceeds the
    hard cap, since an ayah is never split.
    """
    if not batch:
        return batch, audio_files, audio_durations

    kept = 1
    cumulative = audio_durations[0]
    for i in range(1, len(batch)):
        projected = cumulative + audio_durations[i]
        if projected > hard_max:
            break
        cumulative = projected
        kept += 1
        if cumulative >= target_max:
            break

    if kept < len(batch):
        log.info(
            "Trimming batch from %d to %d ayah(s) to stay within the %.0fs Reel target "
            "(%.1fs -> %.1fs). Remaining ayah(s) carry over to the next run.",
            len(batch), kept, hard_max, sum(audio_durations), cumulative,
        )
    if cumulative > hard_max:
        log.warning(
            "Ayah %s:%s alone is %.1fs, exceeding the %.0fs target — keeping it "
            "uncut since ayahs are never split.",
            batch[0][0], batch[0][1], cumulative, hard_max,
        )
    elif cumulative < target_max and kept == len(batch):
        # Ran out of fetched/available ayahs (surah ended, or the fetch
        # pool from audio_downloader.get_next_batch() was too small)
        # before reaching the target floor. Not an error — some surahs
        # are simply short — but worth flagging since it's the direct
        # cause of a video landing under the intended duration window.
        log.warning(
            "Batch exhausted at %.1fs, short of the %.0fs target floor — "
            "surah/fetch pool ended before reaching it. If this happens "
            "often, increase AYAH_PER_VIDEO in audio_downloader.py.",
            cumulative, target_max,
        )

    return batch[:kept], audio_files[:kept], audio_durations[:kept]


def build_video_filter(with_zoom: bool, total_duration: float) -> str:
    """
    Builds the ffmpeg -vf chain for the main-segment composite: fit to
    the target canvas, optionally apply a slow whole-video cinematic
    zoom, then burn in subtitles.

    The zoom rate is derived from total_duration so a short Reel and a
    long Reel both reach CINEMATIC_ZOOM_MAX exactly by the final frame,
    rather than zooming at a fixed per-frame rate that would look
    faster/slower depending on ayah length.
    """
    filters = [
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase",
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}",
        f"fps={VIDEO_FPS}",
    ]
    if with_zoom:
        total_frames = max(VIDEO_FPS * total_duration, 1.0)
        zoom_increment = (CINEMATIC_ZOOM_MAX - 1.0) / total_frames
        filters.append(
            f"zoompan=z='min(zoom+{zoom_increment:.8f},{CINEMATIC_ZOOM_MAX})':"
            f"d=1:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={VIDEO_FPS}"
        )
    filters.append("ass=/tmp/subs.ass")
    return ",".join(filters)


def _run_render(bg_path: Path, audio_path: Path, total_duration: float,
                 video_filter: str, codec_args: list, max_bitrate: int,
                 out_path: Path) -> subprocess.CompletedProcess:
    cmd = [
        "ffmpeg", "-y", "-i", str(bg_path), "-i", str(audio_path),
        "-t", str(total_duration), "-vf", video_filter,
        *codec_args,
        "-maxrate", f"{max_bitrate}k", "-bufsize", f"{max_bitrate * 2}k",
        "-pix_fmt", "yuv420p",
        # Single-pass loudness normalization to the standard short-form
        # social target (~-14 LUFS). This is a final polish pass only —
        # real mastering (de-essing, warmth, harshness removal) happens
        # upstream in audio_processor.master_audio(); TP=-1.5 keeps a
        # true-peak safety margin so this pass never introduces
        # clipping (item 6: "do not introduce clipping").
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", str(AUDIO_SAMPLE_RATE),
        "-movflags", "+faststart", "-shortest", str(out_path),
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def merge_main_segment(bg_path: Path, audio_path: Path, subtitle_path: Path,
                        out_path: Path, total_duration: float) -> None:
    """Composites background + subtitles + mastered audio into the main
    segment (the part whose timing is subtitle-critical — see module
    docstring). This is the single most important encode in the whole
    pipeline — it's where the subtitles actually get burned in and
    where the 2K background, motion, and color grading all become
    final pixels — so it always uses FINAL_ENCODE_CRF/PRESET (item 5 of
    the 2K pass), not the faster/lossier settings used for the
    intermediate per-clip and crossfade passes upstream."""
    safe_sub = Path("/tmp/subs.ass")
    shutil.copy(subtitle_path, safe_sub)

    max_bitrate = int((TARGET_SIZE_MB * 8192) / total_duration)
    encoder = detect_hw_encoder()
    codec_args = (
        ["-c:v", "libx264", "-preset", FINAL_ENCODE_PRESET, "-crf", str(FINAL_ENCODE_CRF)]
        if encoder == "libx264"
        else ["-c:v", encoder, "-b:v", f"{max_bitrate}k"]
    )

    video_filter = build_video_filter(with_zoom=ENABLE_CINEMATIC_ZOOM, total_duration=total_duration)

    log.info("Rendering main segment (%dx%d @ %dfps, encoder=%s, zoom=%s, target <%dMB)...",
              VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, encoder, ENABLE_CINEMATIC_ZOOM, TARGET_SIZE_MB)
    result = _run_render(bg_path, audio_path, total_duration, video_filter, codec_args, max_bitrate, out_path)

    if result.returncode != 0 and encoder != "libx264":
        log.warning("Hardware encode failed, retrying with libx264: %s", result.stderr[-300:])
        encoder = "libx264"
        codec_args = ["-c:v", "libx264", "-preset", FINAL_ENCODE_PRESET, "-crf", str(FINAL_ENCODE_CRF)]
        result = _run_render(bg_path, audio_path, total_duration, video_filter, codec_args, max_bitrate, out_path)

    if result.returncode != 0 and ENABLE_CINEMATIC_ZOOM:
        log.warning("Render with cinematic zoom failed, retrying without it: %s", result.stderr[-300:])
        video_filter = build_video_filter(with_zoom=False, total_duration=total_duration)
        result = _run_render(bg_path, audio_path, total_duration, video_filter, codec_args, max_bitrate, out_path)

    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg render failed: {result.stderr[-500:]}")

    size_mb = out_path.stat().st_size / 1024 / 1024
    log.info("Main segment size: %.1f MB", size_mb)


def with_retries(stage_name: str, fn, *args, **kwargs):
    last_error = None
    for attempt in range(1, MAX_STAGE_RETRIES + 2):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 — deliberately broad: pipeline stage boundary
            last_error = e
            log.warning("Stage '%s' failed (attempt %d/%d): %s",
                        stage_name, attempt, MAX_STAGE_RETRIES + 1, e)
            time.sleep(min(2 ** attempt, 20))
    raise RuntimeError(f"Stage '{stage_name}' failed after retries: {last_error}") from last_error


def _duration_bucket_name(target_max: float, hard_max: float) -> str:
    for name, (t, h) in config.DURATION_BUCKETS.items():
        if (t, h) == (target_max, hard_max):
            return name
    return "custom"


def main():
    with tempfile.TemporaryDirectory() as _tmp:
        tmpdir = Path(_tmp)
        arabic_data = load_json(ARABIC_JSON)
        english_data = load_json(ENGLISH_JSON)

        batch, surah_en, surah_ar = get_next_batch()
        qa_gate.validate_batch_text(batch, arabic_data, english_data)

        audio_files, audio_durations = with_retries("download audio", download_batch, batch, tmpdir)

        # Duration experimentation (item 12): pick a target/hard-max
        # window for this run, biased toward whatever has actually
        # performed best once there's enough data — see
        # performance_metadata.pick_duration_bucket(). Ayah integrity
        # always wins regardless of which bucket comes back.
        try:
            target_max, hard_max = pick_duration_bucket()
        except Exception as e:  # noqa: BLE001 — never let experimentation bookkeeping block a render
            log.warning("Duration bucket selection failed (%s) — using default window.", e)
            target_max, hard_max = DEFAULT_TARGET_MAX_DURATION, DEFAULT_HARD_MAX_DURATION

        batch, audio_files, audio_durations = fit_batch_to_duration(
            batch, audio_files, audio_durations, target_max, hard_max,
        )
        total_duration = sum(audio_durations)

        raw_audio = tmpdir / "combined_audio_raw.mp3"
        concat_audio(audio_files, raw_audio)

        mastered_audio = tmpdir / "combined_audio.aac"
        with_retries("master audio", master_audio, raw_audio, mastered_audio, total_duration)

        # Subtitles: frozen subsystem, called exactly as before this
        # upgrade with no changes to its inputs, outputs, or timing.
        subtitle_file = tmpdir / "subtitles.ass"
        build_subtitles(batch, arabic_data, english_data, audio_durations, subtitle_file)

        # Visual mood engine (item 3): pick a mood from the batch's
        # English translation text, then a template/category/motion/
        # color-grade/transition plan to match.
        english_texts = [get_ayah_text(english_data, s, a) for s, a in batch]
        plan = choose_plan(english_texts)

        bg_path = tmpdir / "background.mp4"
        try:
            bg_result = with_retries(
                "build background", build_background_for_plan, plan, total_duration, tmpdir, bg_path,
            )
        except PexelsError as e:
            log.error("Background pipeline exhausted retries: %s", e)
            raise
        motion_styles_used = bg_result["motion_styles"]
        source_clips = bg_result["source_clips"]

        main_segment = tmpdir / "main_segment.mp4"
        merge_main_segment(bg_path, mastered_audio, subtitle_file, main_segment, total_duration)

        # QA the main segment BEFORE anything intro-related touches it —
        # this is what actually guarantees subtitle sync, independent of
        # whatever the intro does.
        qa_gate.validate_main_segment(main_segment, subtitle_file, total_duration)

        # Cinematic Bismillah intro (item 1) — built and joined on only
        # after the main segment is already complete and validated. See
        # intro_builder.py and the module docstring above for why this
        # ordering can never affect subtitle timing.
        intro_info = None
        final_duration = total_duration
        if INTRO_ENABLED:
            try:
                intro_path = tmpdir / "intro.mp4"
                intro_info = with_retries("build intro", build_intro, tmpdir, intro_path)
            except IntroBuildError as e:
                log.warning("Intro build failed (%s) — continuing without an intro this run.", e)
                intro_info = None

        if intro_info:
            join_intro_and_main(intro_info["path"], intro_info["duration"], main_segment, OUTPUT_VIDEO)
            # A crossfade join overlaps the tail of the intro with the
            # head of the main segment, so the combined duration is
            # slightly less than the simple sum.
            final_duration = total_duration + intro_info["duration"] - INTRO_JOIN_TRANSITION
        else:
            shutil.copy(main_segment, OUTPUT_VIDEO)

        # Final QA gate (item 17) — never write metadata / advance
        # progress / hand a video to upload.py unless the rendered file
        # actually checks out end to end.
        qa_gate.validate_final_deliverable(OUTPUT_VIDEO, subtitle_file, final_duration)

        title = f"Surah {surah_en} {batch[0][0]}:{batch[0][1]}-{batch[-1][1]} | Quran"

        # Canonical video identity (item 4: real analytics feedback
        # loop) — a sha256 of the FINAL rendered file, computed once
        # here and reused by upload.py for the exact same file, so a
        # video's generation record, its platform ID, and its eventual
        # real performance numbers all key off one consistent value
        # with no fragile title-matching.
        try:
            video_hash = compute_video_hash(OUTPUT_VIDEO)
        except OSError as e:
            log.warning("Could not hash %s for analytics (%s) — analytics linkage will be skipped this run.",
                        OUTPUT_VIDEO, e)
            video_hash = ""

        metadata = build_metadata(
            surah_num=batch[0][0], surah_name=surah_en,
            first_ayah=batch[0][1], last_ayah=batch[-1][1],
            duration=final_duration, video_file=str(OUTPUT_VIDEO),
            visual_template=plan["visual_template"], visual_mood=plan["mood"],
            visual_category=plan["visual_category"], motion_styles=motion_styles_used,
            transition_style=plan["transition_style"], color_grade=plan["color_grade"],
            intro_enabled=intro_info is not None,
            intro_duration=intro_info["duration"] if intro_info else 0.0,
            duration_bucket=_duration_bucket_name(target_max, hard_max), title=title,
            video_hash=video_hash,
            render_width=VIDEO_WIDTH, render_height=VIDEO_HEIGHT,
            source_clips=source_clips, codec="h264", pixel_format="yuv420p", audio_codec="aac",
        )
        with open(METADATA_FILE, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

        # Record this render for the experimentation framework (item 14).
        # A real performance number isn't available yet at generation
        # time — see performance_metadata.record_performance() and
        # analytics_ingest.py for the separate, standalone step that
        # fetches real platform numbers later and calls it.
        if video_hash:
            try:
                record_generation(video_hash, metadata)
            except Exception as e:  # noqa: BLE001 — analytics bookkeeping must never block a render
                log.warning("Failed to record generation metadata for analytics: %s", e)

        # CRITICAL: advance progress as soon as a *validated* video exists,
        # not after upload.py runs. Progress must never depend on whether
        # YouTube/Facebook/Instagram upload succeeds — otherwise a single
        # upload failure (or unconfigured credentials) freezes the whole
        # pipeline on the same batch forever, which is exactly what caused
        # the "always the same 7 ayahs" bug. Uses the (possibly trimmed)
        # batch, so any ayahs left out by fit_batch_to_duration() are
        # correctly picked up on the next run rather than skipped.
        save_progress(batch[0][0], batch[-1][1])

        log.info("Pipeline complete: Surah %s %d:%d-%d (mood=%s, template=%s, intro=%s)",
                  surah_en, batch[0][0], batch[0][1], batch[-1][1],
                  plan["mood"], plan["visual_template"], intro_info is not None)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log.error("FATAL: build pipeline failed: %s", e)
        sys.exit(1)
