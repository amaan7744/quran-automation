#!/usr/bin/env python3
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

def merge_final_video(bg_path, audio_path, subtitle_path, out_path, total_duration):
    """Optimized for Meta 100MB limit with CRF 22 and bitrate safety."""
    safe_sub = Path("/tmp/subs.ass")
    shutil.copy(subtitle_path, safe_sub)

    # Calculate safe bitrate to target ~90MB (Buffer for safety)
    max_bitrate = int((90 * 8192) / total_duration)

    video_filter = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
        f"ass=/tmp/subs.ass"
    )

    cmd = [
        "ffmpeg", "-y", "-i", str(bg_path), "-i", str(audio_path),
        "-t", str(total_duration), "-vf", video_filter,
        "-c:v", "libx264", "-preset", "slow", "-crf", "22", 
        "-maxrate", f"{max_bitrate}k", "-bufsize", f"{max_bitrate*2}k",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart", "-shortest", str(out_path)
    ]
    
    print(f"  Merging video (Targeting <100MB for Instagram)...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {result.stderr[-500:]}")
    
    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"  Final video size: {size_mb:.1f} MB")

def main():
    with tempfile.TemporaryDirectory() as _tmp:
        tmpdir = Path(_tmp)
        arabic_data = load_json(ARABIC_JSON)
        english_data = load_json(ENGLISH_JSON)

        batch, surah_en, surah_ar = get_next_batch()
        audio_files, audio_durations = download_batch(batch, tmpdir)
        total_duration = sum(audio_durations)

        combined_audio = tmpdir / "combined_audio.mp3"
        concat_audio(audio_files, combined_audio)

        subtitle_file = tmpdir / "subtitles.ass"
        build_subtitles(batch, arabic_data, english_data, audio_durations, subtitle_file)

        bg_path = tmpdir / "background.mp4"
        clips = fetch_clips_for_duration(total_duration)
        download_and_concat_clips(clips, tmpdir, bg_path, total_duration)

        merge_final_video(bg_path, combined_audio, subtitle_file, OUTPUT_VIDEO, total_duration)

        # Generate Metadata LAST so upload.py can find it
        with open(METADATA_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "title": f"Surah {surah_en} {batch[0][0]}:{batch[0][1]}-{batch[-1][1]} | Quran",
                "surah_num": batch[0][0],
                "last_ayah": batch[-1][1],
                "video_file": str(OUTPUT_VIDEO)
            }, f, indent=2)

if __name__ == "__main__":
    main()
