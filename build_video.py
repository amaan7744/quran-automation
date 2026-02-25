#!/usr/bin/env python3
"""
build_video.py
Optimized for Meta 100MB limits.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from audio_downloader import get_next_batch, download_batch, concat_audio, save_progress, RECITER_NAME
from pexels_fetcher   import fetch_clips_for_duration, download_and_concat_clips
from subtitle_builder import build_subtitles

VIDEO_WIDTH   = 1440
VIDEO_HEIGHT  = 2560
ARABIC_JSON   = Path("arabic.json")
ENGLISH_JSON  = Path("english.json")
OUTPUT_VIDEO  = Path("output_video.mp4")
METADATA_FILE = Path("video_metadata.json")

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
    Targeting <100MB for Instagram compatibility using CRF 22 and bitrate caps.
    """
    safe_sub = Path("/tmp/subs.ass")
    shutil.copy(subtitle_path, safe_sub)

    # Safety Math: Target 90MB to leave a buffer for metadata
    # (90MB * 8192) / total_seconds = max_kbps
    max_bitrate = int((90 * 8192) / total_duration)

    video_filter = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
        f"ass=/tmp/subs.ass"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(bg_path),
        "-i", str(audio_path),
        "-t", str(total_duration),
        "-vf", video_filter,
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "22",               # Reduced from 16 to 22 for better compression
        "-maxrate", f"{max_bitrate}k", # Prevents files exceeding 100MB
        "-bufsize", f"{max_bitrate*2}k",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",              # Switched to aac for better Meta compatibility
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-shortest",
        str(out_path),
    ]

    print(f"  Running FFmpeg merge (Targeting under 100MB)...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("FFmpeg stderr:")
        print(result.stderr[-3000:])
        raise RuntimeError("FFmpeg failed.")

    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"  Final video -> {out_path} ({size_mb:.1f} MB)")

# ... (generate_title and generate_description functions remain unchanged)

def main():
    with tempfile.TemporaryDirectory() as _tmp:
        tmpdir = Path(_tmp)

        print("Loading JSON data...")
        arabic_data = load_json(ARABIC_JSON)
        english_data = load_json(ENGLISH_JSON)

        print("\nDetermining next ayah batch...")
        batch, surah_name_en, surah_name_ar = get_next_batch()
        surah_num = batch[0][0]

        print("\nDownloading and processing audio...")
        audio_files, audio_durations = download_batch(batch, tmpdir)
        total_duration = sum(audio_durations)
        combined_audio = tmpdir / "combined_audio.mp3"
        concat_audio(audio_files, combined_audio)

        print("\nBuilding and verifying subtitles...")
        subtitle_file = tmpdir / "subtitles.ass"
        build_subtitles(batch, arabic_data, english_data, audio_durations, subtitle_file)

        print("\nFetching Pexels background...")
        bg_path = tmpdir / "background.mp4"
        clips = fetch_clips_for_duration(total_duration)
        download_and_concat_clips(clips, tmpdir, bg_path, total_duration)

        print("\nMerging final video...")
        merge_final_video(bg_path, combined_audio, subtitle_file, OUTPUT_VIDEO, total_duration)

        # Save Metadata and Progress
        # (Same as your original script logic)
        # ...
        print(f"\nDone! Video: {OUTPUT_VIDEO}")

if __name__ == "__main__":
    main()
