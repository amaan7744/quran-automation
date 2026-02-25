#!/usr/bin/env python3
"""
build_video.py
Full pipeline: audio -> subtitles -> pexels background -> final 2K video
Subtitles are burned from arabic.json and english.json directly.
Progress saved LAST after everything succeeds.
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


def verify_subtitles(subtitle_path: Path, batch: list) -> None:
    """Print subtitle file content to logs so we can confirm it was built correctly."""
    content = subtitle_path.read_text(encoding="utf-8")
    lines   = [l for l in content.splitlines() if l.startswith("Dialogue")]
    print(f"  Subtitle lines generated: {len(lines)}")
    if lines:
        print(f"  First subtitle line: {lines[0][:120]}")
    else:
        raise RuntimeError("Subtitle file has NO Dialogue lines — arabic.json or english.json may be empty or wrong format.")


def merge_final_video(
    bg_path:        Path,
    audio_path:     Path,
    subtitle_path:  Path,
    out_path:       Path,
    total_duration: float,
) -> None:
    """
    Merge background + audio + burned subtitles into final 2K video.
    Subtitle path is copied to a simple name with no spaces or special chars
    to ensure FFmpeg ass filter works correctly on Linux.
    """
    # Copy subtitle to a safe simple path — avoids ALL escaping issues
    safe_sub = Path("/tmp/subs.ass")
    shutil.copy(subtitle_path, safe_sub)

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
        "-crf", "16",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-shortest",
        str(out_path),
    ]

    print("  Running FFmpeg merge...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("FFmpeg stderr:")
        print(result.stderr[-3000:])  # print last 3000 chars of error
        raise RuntimeError("FFmpeg failed. See logs above.")

    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"  Final video -> {out_path} ({size_mb:.1f} MB)")


def generate_title(surah_name_en: str, surah_num: int, batch: list) -> str:
    first = batch[0][1]
    last  = batch[-1][1]
    # Short punchy title optimised for Shorts — under 60 chars
    return f"Surah {surah_name_en} {surah_num}:{first}-{last} | {RECITER_NAME} | Quran"


def generate_description(surah_name_en, surah_name_ar, surah_num, batch):
    first = batch[0][1]
    last  = batch[-1][1]
    return (
        f"{surah_name_ar} | {surah_name_en} — Ayah {first} to {last}\n"
        f"Recited by {RECITER_NAME}\n\n"
        f"Listen, reflect, and share. May Allah make it a source of reward.\n\n"
        f"Arabic: Tanzil | English: Sahih International\n\n"
        f"#quran #quranrecitation #quranshorts #islam #islamicvideo #shorts "
        f"#dailyquran #qurankareem #muslimshorts #islamicreminder "
        f"#{surah_name_en.replace(' ', '').lower()} #qurantilawat #saudialshuraim"
    )


def main():
    with tempfile.TemporaryDirectory() as _tmp:
        tmpdir = Path(_tmp)

        # ── Load arabic.json and english.json ─────────────────────────────────
        print("Loading arabic.json...")
        arabic_data = load_json(ARABIC_JSON)
        print(f"  Arabic entries loaded: {len(arabic_data) if isinstance(arabic_data, list) else len(arabic_data)} items")

        print("Loading english.json...")
        english_data = load_json(ENGLISH_JSON)
        print(f"  English entries loaded: {len(english_data) if isinstance(english_data, list) else len(english_data)} items")

        # ── Get next batch ────────────────────────────────────────────────────
        print("\nDetermining next ayah batch...")
        batch, surah_name_en, surah_name_ar = get_next_batch()
        surah_num = batch[0][0]
        print(f"  Will process: {[(s,a) for s,a in batch]}")

        # ── Download audio ────────────────────────────────────────────────────
        print("\nDownloading ayah audio...")
        audio_files, audio_durations = download_batch(batch, tmpdir)
        total_duration = sum(audio_durations)
        print(f"  Total duration: {total_duration:.2f}s")

        # ── Concat audio ──────────────────────────────────────────────────────
        print("\nConcatenating audio (no re-encode)...")
        combined_audio = tmpdir / "combined_audio.mp3"
        concat_audio(audio_files, combined_audio)

        # ── Build subtitles from JSON ─────────────────────────────────────────
        print("\nBuilding subtitles from arabic.json + english.json...")
        subtitle_file = tmpdir / "subtitles.ass"
        build_subtitles(batch, arabic_data, english_data, audio_durations, subtitle_file)

        # Verify subtitles were actually written with content
        verify_subtitles(subtitle_file, batch)

        # ── Fetch Pexels clips ────────────────────────────────────────────────
        print("\nFetching Pexels nature clips...")
        bg_path = tmpdir / "background.mp4"
        clips   = fetch_clips_for_duration(total_duration)
        download_and_concat_clips(clips, tmpdir, bg_path, total_duration)

        # ── Merge final video ─────────────────────────────────────────────────
        print("\nMerging final 2K video with subtitles...")
        merge_final_video(bg_path, combined_audio, subtitle_file, OUTPUT_VIDEO, total_duration)

        # ── Save metadata ─────────────────────────────────────────────────────
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
        print(f"  Metadata saved -> {METADATA_FILE}")

        # ── Save progress LAST (only after full success) ──────────────────────
        print("\nSaving progress...")
        save_progress(surah_num, batch[-1][1])

        print(f"\nDone!")
        print(f"  Video : {OUTPUT_VIDEO}")
        print(f"  Title : {title}")


if __name__ == "__main__":
    main()
