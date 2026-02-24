#!/usr/bin/env python3
"""
build_video.py
Full pipeline orchestrator:
  1. Get next ayah batch (audio_downloader.py)
  2. Download audio (audio_downloader.py)
  3. Build subtitles (subtitle_builder.py)
  4. Fetch + concat multiple Pexels clips (pexels_fetcher.py)
  5. Burn subtitles + upscale to 2K final video
  6. Save metadata for upload.py
  7. Save progress LAST (only on full success = no repeats ever)

Quality: 1440x2560 (2K 9:16), CRF 16, slow preset, yuv420p
Subtitles: burned directly in, always visible with background box
Background: multiple random short Pexels clips concatenated, never same twice
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from surah_data       import SURAHS
from audio_downloader import get_next_batch, download_batch, concat_audio, save_progress, RECITER_NAME
from pexels_fetcher   import fetch_clips_for_duration, download_and_concat_clips
from subtitle_builder import build_subtitles

# ─── CONFIG ────────────────────────────────────────────────────────────────────
VIDEO_WIDTH   = 1440
VIDEO_HEIGHT  = 2560

ARABIC_JSON   = Path("arabic.json")
ENGLISH_JSON  = Path("english.json")
OUTPUT_VIDEO  = Path("output_video.mp4")
METADATA_FILE = Path("video_metadata.json")
# ───────────────────────────────────────────────────────────────────────────────


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def merge_final_video(
    bg_path:        Path,
    audio_path:     Path,
    subtitle_path:  Path,
    out_path:       Path,
    total_duration: float,
) -> None:
    """
    Merge background + audio + burned subtitles into final 2K video.
    - Background already normalized to 1440x2560 by pexels_fetcher
    - Subtitles burned with ass filter (always visible, opaque box)
    - Audio copied as-is (zero quality loss)
    - CRF 16 = very high quality
    - yuv420p = universal platform compatibility
    - faststart = instant playback on mobile
    """
    # Escape subtitle path for FFmpeg ass filter on Linux
    sub_escaped = str(subtitle_path.resolve()).replace("\\", "/").replace(":", "\\:")

    video_filter = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
        f"ass={sub_escaped}"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(bg_path),
        "-i", str(audio_path),
        "-t", str(total_duration),
        "-vf", video_filter,
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "16",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-shortest",
        str(out_path),
    ]

    print("  Running FFmpeg final merge...")
    subprocess.run(cmd, check=True)
    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"  Final video -> {out_path} ({size_mb:.1f} MB)")


def generate_title(surah_name_en: str, surah_num: int, batch: list) -> str:
    first = batch[0][1]
    last  = batch[-1][1]
    return f"Quran | {surah_name_en} ({surah_num}:{first}-{last}) | {RECITER_NAME}"


def generate_description(
    surah_name_en: str,
    surah_name_ar: str,
    surah_num:     int,
    batch:         list,
) -> str:
    first = batch[0][1]
    last  = batch[-1][1]
    return (
        f"Quran Recitation | {surah_name_ar} - {surah_name_en} (Surah {surah_num})\n"
        f"Ayah {first}-{last}\n\n"
        f"Recited by: {RECITER_NAME}\n"
        f"Arabic Text: Tanzil\n"
        f"English Translation: Sahih International\n\n"
        f"#Quran #QuranRecitation #{surah_name_en.replace(' ', '')} #Islam #DailyQuran"
    )


def main():
    with tempfile.TemporaryDirectory() as _tmp:
        tmpdir = Path(_tmp)

        # ── Load Quran text ───────────────────────────────────────────────────
        print("Loading Quran text...")
        arabic_data  = load_json(ARABIC_JSON)
        english_data = load_json(ENGLISH_JSON)

        # ── Get next batch (strict order, no repeats) ─────────────────────────
        print("\nDetermining next ayah batch...")
        batch, surah_name_en, surah_name_ar = get_next_batch()
        surah_num = batch[0][0]

        # ── Download ayah audio ───────────────────────────────────────────────
        print("\nDownloading ayah audio from everyayah.com...")
        audio_files, audio_durations = download_batch(batch, tmpdir)
        total_duration = sum(audio_durations)
        print(f"  Total audio: {total_duration:.2f}s")

        # ── Concatenate audio (no re-encode) ──────────────────────────────────
        print("\nConcatenating audio...")
        combined_audio = tmpdir / "combined_audio.mp3"
        concat_audio(audio_files, combined_audio)

        # ── Build subtitles ───────────────────────────────────────────────────
        print("\nBuilding subtitles...")
        subtitle_file = tmpdir / "subtitles.ass"
        build_subtitles(batch, arabic_data, english_data, audio_durations, subtitle_file)

        # ── Fetch multiple short Pexels clips and concat ──────────────────────
        print("\nFetching Pexels nature clips...")
        bg_path = tmpdir / "background.mp4"
        clips   = fetch_clips_for_duration(total_duration)
        download_and_concat_clips(clips, tmpdir, bg_path, total_duration)

        # ── Merge final 2K video ──────────────────────────────────────────────
        print("\nMerging final 2K video...")
        merge_final_video(
            bg_path, combined_audio, subtitle_file, OUTPUT_VIDEO, total_duration
        )

        # ── Save metadata for upload.py ───────────────────────────────────────
        title       = generate_title(surah_name_en, surah_num, batch)
        description = generate_description(surah_name_en, surah_name_ar, surah_num, batch)

        with open(METADATA_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "title":       title,
                "description": description,
                "surah_num":   surah_num,
                "surah_en":    surah_name_en,
                "surah_ar":    surah_name_ar,
                "first_ayah":  batch[0][1],
                "last_ayah":   batch[-1][1],
                "video_file":  str(OUTPUT_VIDEO),
            }, f, ensure_ascii=False, indent=2)
        print(f"  Metadata -> {METADATA_FILE}")

        # ── Save progress LAST — only after everything succeeded ──────────────
        print("\nSaving progress...")
        save_progress(surah_num, batch[-1][1])

        print(f"\nDone!")
        print(f"  Video : {OUTPUT_VIDEO}")
        print(f"  Title : {title}")


if __name__ == "__main__":
    main()


def save_metadata(
    title:         str,
    description:   str,
    surah_name_en: str,
    surah_name_ar: str,
    surah_num:     int,
    batch:         list,
) -> None:
    """Save video metadata for upload.py to read."""
    with open(METADATA_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "title":       title,
            "description": description,
            "surah_num":   surah_num,
            "surah_en":    surah_name_en,
            "surah_ar":    surah_name_ar,
            "first_ayah":  batch[0][1],
            "last_ayah":   batch[-1][1],
            "video_file":  str(OUTPUT_VIDEO),
        }, f, ensure_ascii=False, indent=2)
    print(f"  Metadata saved -> {METADATA_FILE}")
