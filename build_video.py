#!/usr/bin/env python3
"""
build_video.py
Orchestrates the full pipeline: fetch ayah audio -> master audio ->
build karaoke subtitles -> fetch/edit a moving nature background ->
composite the final 1080x1920 video -> write metadata for upload.py.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from audio_downloader import get_next_batch, download_batch, concat_audio, save_progress
from pexels_fetcher import build_background, PexelsError
from subtitle_builder import build_subtitles, get_ayah_text
from audio_processor import master_audio
from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, TARGET_SIZE_MB
from logging_utils import get_logger

log = get_logger(__name__)

ARABIC_JSON   = Path("arabic.json")
ENGLISH_JSON  = Path("english.json")
OUTPUT_VIDEO  = Path("output_video.mp4")
METADATA_FILE = Path("video_metadata.json")

MAX_STAGE_RETRIES = 2


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


def merge_final_video(bg_path: Path, audio_path: Path, subtitle_path: Path,
                       out_path: Path, total_duration: float) -> None:
    """Composites background + subtitles + mastered audio into the final deliverable."""
    safe_sub = Path("/tmp/subs.ass")
    shutil.copy(subtitle_path, safe_sub)

    max_bitrate = int((TARGET_SIZE_MB * 8192) / total_duration)
    encoder = detect_hw_encoder()

    video_filter = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
        f"fps={VIDEO_FPS},"
        f"ass=/tmp/subs.ass"
    )

    if encoder == "libx264":
        codec_args = ["-c:v", "libx264", "-preset", "slow", "-crf", "20"]
    else:
        # Hardware encoders don't support CRF the same way — drive with bitrate instead.
        codec_args = ["-c:v", encoder, "-b:v", f"{max_bitrate}k"]

    cmd = [
        "ffmpeg", "-y", "-i", str(bg_path), "-i", str(audio_path),
        "-t", str(total_duration), "-vf", video_filter,
        *codec_args,
        "-maxrate", f"{max_bitrate}k", "-bufsize", f"{max_bitrate * 2}k",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", "-shortest", str(out_path),
    ]

    log.info("Rendering final video (%dx%d @ %dfps, encoder=%s, target <%dMB)...",
              VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, encoder, TARGET_SIZE_MB)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        if encoder != "libx264":
            log.warning("Hardware encode failed, retrying with libx264: %s", result.stderr[-300:])
            codec_args = ["-c:v", "libx264", "-preset", "slow", "-crf", "20"]
            cmd = [
                "ffmpeg", "-y", "-i", str(bg_path), "-i", str(audio_path),
                "-t", str(total_duration), "-vf", video_filter,
                *codec_args,
                "-maxrate", f"{max_bitrate}k", "-bufsize", f"{max_bitrate * 2}k",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", "-shortest", str(out_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg render failed: {result.stderr[-500:]}")

    size_mb = out_path.stat().st_size / 1024 / 1024
    log.info("Final video size: %.1f MB", size_mb)


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
        raise RuntimeError(
            f"Ayah/translation text missing for: {', '.join(missing)}. "
            "Refusing to build a video with mismatched or missing text."
        )
    log.info("Validated Arabic + English text present for all %d ayah(s) in batch.", len(batch))


def probe_video(path: Path) -> dict:
    """Returns {width, height, duration, has_audio} for a rendered mp4."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {r.stderr[-300:]}")
    info = json.loads(r.stdout)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return {
        "width": v.get("width"),
        "height": v.get("height"),
        "duration": float(info.get("format", {}).get("duration", 0.0)),
        "has_audio": has_audio,
    }


def validate_final_output(out_path: Path, subtitle_path: Path, expected_duration: float) -> None:
    """
    Final quality gate: the pipeline must NOT hand a video to upload.py
    unless it actually looks right. Checks correct resolution, presence
    of an audio track, duration matching what the ayah audio should be,
    and a non-empty subtitle track (so we never publish a silent/blank
    or badly-cropped video, or one whose subtitles failed to render).
    """
    if not out_path.exists() or out_path.stat().st_size < 100_000:
        raise RuntimeError(f"Final video missing or suspiciously small: {out_path}")

    info = probe_video(out_path)
    if info["width"] != VIDEO_WIDTH or info["height"] != VIDEO_HEIGHT:
        raise RuntimeError(
            f"Final video resolution {info['width']}x{info['height']} != "
            f"expected {VIDEO_WIDTH}x{VIDEO_HEIGHT}"
        )
    if not info["has_audio"]:
        raise RuntimeError("Final video has no audio stream.")
    if abs(info["duration"] - expected_duration) > 1.5:
        raise RuntimeError(
            f"Final video duration {info['duration']:.2f}s doesn't match "
            f"expected recitation duration {expected_duration:.2f}s — "
            "possible audio/video desync."
        )
    if not subtitle_path.exists() or subtitle_path.stat().st_size < 50:
        raise RuntimeError("Subtitle file is missing or empty.")

    log.info("Validation passed: %dx%d, %.2fs, audio present, subtitles present.",
              info["width"], info["height"], info["duration"])


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


def main():
    with tempfile.TemporaryDirectory() as _tmp:
        tmpdir = Path(_tmp)
        arabic_data = load_json(ARABIC_JSON)
        english_data = load_json(ENGLISH_JSON)

        batch, surah_en, surah_ar = get_next_batch()
        validate_batch_text(batch, arabic_data, english_data)

        audio_files, audio_durations = with_retries("download audio", download_batch, batch, tmpdir)
        total_duration = sum(audio_durations)

        raw_audio = tmpdir / "combined_audio_raw.mp3"
        concat_audio(audio_files, raw_audio)

        mastered_audio = tmpdir / "combined_audio.aac"
        with_retries("master audio", master_audio, raw_audio, mastered_audio, total_duration)

        subtitle_file = tmpdir / "subtitles.ass"
        build_subtitles(batch, arabic_data, english_data, audio_durations, subtitle_file)

        bg_path = tmpdir / "background.mp4"
        try:
            with_retries("build background", build_background, total_duration, tmpdir, bg_path)
        except PexelsError as e:
            log.error("Background pipeline exhausted retries: %s", e)
            raise

        merge_final_video(bg_path, mastered_audio, subtitle_file, OUTPUT_VIDEO, total_duration)

        # Quality gate — never write metadata / advance progress / hand a
        # video to upload.py unless the rendered file actually checks out.
        validate_final_output(OUTPUT_VIDEO, subtitle_file, total_duration)

        with open(METADATA_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "title": f"Surah {surah_en} {batch[0][0]}:{batch[0][1]}-{batch[-1][1]} | Quran",
                "surah_num": batch[0][0],
                "surah_name": surah_en,
                "last_ayah": batch[-1][1],
                "first_ayah": batch[0][1],
                "video_file": str(OUTPUT_VIDEO),
                "duration": total_duration,
            }, f, indent=2)

        # CRITICAL: advance progress as soon as a *validated* video exists,
        # not after upload.py runs. Progress must never depend on whether
        # YouTube/Facebook/Instagram upload succeeds — otherwise a single
        # upload failure (or unconfigured credentials) freezes the whole
        # pipeline on the same batch forever, which is exactly what caused
        # the "always the same 7 ayahs" bug.
        save_progress(batch[0][0], batch[-1][1])

        log.info("Pipeline complete: Surah %s %d:%d-%d", surah_en, batch[0][0], batch[0][1], batch[-1][1])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log.error("FATAL: build pipeline failed: %s", e)
        sys.exit(1)
