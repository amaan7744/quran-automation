#!/usr/bin/env python3
"""
build_video.py
Single file that does the full pipeline:
  1. Read progress.json to find next batch of ayahs
  2. Download ayah audio from everyayah.com
  3. Concatenate audio (no re-encode)
  4. Build Arabic + English ASS subtitle file
  5. Fetch nature background video from Pexels
  6. Merge everything into a 2K 9:16 MP4
  7. Save video_metadata.json for upload.py
  8. Save progress.json (only after full success — no repeat, no skip)
"""

import json
import os
import random
import subprocess
import tempfile
import textwrap
import requests
from pathlib import Path

from surah_data import SURAHS

# ─── CONFIG ────────────────────────────────────────────────────────────────────
RECITER_FOLDER  = "Saood_ash-Shuraym_128kbps"
RECITER_NAME    = "Saad Al-Ghamdi"
EVERYAYAH_BASE  = "https://everyayah.com/data"

AYAH_PER_VIDEO  = 7     # ayahs per video for large surahs
SMALL_SURAH_MAX = 10    # surahs with <= this many ayahs done in one video

# 2K vertical resolution for Reels / Shorts (1440x2560 = QHD 9:16)
VIDEO_WIDTH     = 1440
VIDEO_HEIGHT    = 2560

PEXELS_API_KEY  = os.environ["PEXELS_API_KEY"]

PROGRESS_FILE   = Path("progress.json")
ARABIC_JSON     = Path("arabic.json")
ENGLISH_JSON    = Path("english.json")
OUTPUT_VIDEO    = Path("output_video.mp4")
METADATA_FILE   = Path("video_metadata.json")

NATURE_QUERIES  = [
    "nature landscape",
    "forest river",
    "ocean waves",
    "mountain sunrise",
    "waterfall nature",
    "green forest",
    "desert sunset",
    "rain forest",
    "lake reflection",
    "snow mountain",
    "flowing river",
    "clouds sky nature",
    "autumn forest",
    "tropical beach",
    "misty mountains",
]
# ───────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
# PROGRESS
# ══════════════════════════════════════════════════════════════════════════════

def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, "r") as f:
            data = json.load(f)
        data.pop("note", None)   # remove legacy note field if present
        return data
    return {"last_surah": 1, "last_ayah": 0}


def save_progress(surah: int, ayah: int) -> None:
    with open(PROGRESS_FILE, "w") as f:
        json.dump({"last_surah": surah, "last_ayah": ayah}, f, indent=2)
    print(f"  Progress saved -> Surah {surah}, Ayah {ayah}")


# ══════════════════════════════════════════════════════════════════════════════
# BATCH LOGIC
# ══════════════════════════════════════════════════════════════════════════════

def next_batch() -> tuple:
    """
    Determine the exact next set of ayahs to process.
    Never repeats. Never skips. Moves to next surah automatically.

    Returns:
        batch         -- list of (surah_num, ayah_num) in strict order
        surah_name_en -- English surah name
        surah_name_ar -- Arabic surah name
    """
    progress  = load_progress()
    cur_surah = progress["last_surah"]
    cur_ayah  = progress["last_ayah"]

    surah_map = {s[0]: s for s in SURAHS}

    # Get current surah info
    s_num, s_name_en, s_name_ar, total_ayah = surah_map[cur_surah]

    # Next ayah to process
    next_ayah = cur_ayah + 1

    # If current surah is fully done, move to the next surah
    if next_ayah > total_ayah:
        next_surah_num = cur_surah + 1
        if next_surah_num > 114:
            print("  Entire Quran completed — restarting from Surah 1.")
            next_surah_num = 1
        s_num, s_name_en, s_name_ar, total_ayah = surah_map[next_surah_num]
        next_ayah = 1
        print(f"  Surah {cur_surah} done. Moving to Surah {s_num} ({s_name_en}).")

    remaining = total_ayah - next_ayah + 1

    # Small surah: finish all remaining ayahs in one video
    if total_ayah <= SMALL_SURAH_MAX:
        batch_size = remaining
    else:
        batch_size = min(AYAH_PER_VIDEO, remaining)

    batch = [(s_num, a) for a in range(next_ayah, next_ayah + batch_size)]
    print(f"  Batch -> Surah {s_num} ({s_name_en}), Ayahs {next_ayah}–{next_ayah + batch_size - 1}")
    return batch, s_name_en, s_name_ar


# ══════════════════════════════════════════════════════════════════════════════
# AUDIO
# ══════════════════════════════════════════════════════════════════════════════

def audio_url(surah: int, ayah: int) -> str:
    return f"{EVERYAYAH_BASE}/{RECITER_FOLDER}/{surah:03d}{ayah:03d}.mp3"


def download_ayah_audio(surah: int, ayah: int, dest: Path) -> None:
    url = audio_url(surah, ayah)
    print(f"  Audio {surah}:{ayah} <- {url}")
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"everyayah.com returned HTTP {r.status_code} for {url}")
    dest.write_bytes(r.content)


def get_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1",
         str(path)],
        capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def download_all_audio(batch: list, tmpdir: Path) -> tuple:
    """Download all ayahs. Returns (audio_files, durations)."""
    audio_files     = []
    audio_durations = []
    for surah, ayah in batch:
        dest = tmpdir / f"ayah_{surah:03d}_{ayah:03d}.mp3"
        download_ayah_audio(surah, ayah, dest)
        dur = get_duration(dest)
        print(f"    Duration: {dur:.2f}s")
        audio_files.append(dest)
        audio_durations.append(dur)
    return audio_files, audio_durations


def concat_audio(audio_files: list, out_path: Path) -> None:
    """Concatenate MP3s with no re-encode — copy stream directly."""
    list_file = out_path.parent / "audio_list.txt"
    with open(list_file, "w") as f:
        for af in audio_files:
            f.write(f"file '{af.resolve()}'\n")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", str(list_file), "-c", "copy", str(out_path)],
        check=True, capture_output=True,
    )
    print(f"  Audio concatenated -> {out_path.name}")


# ══════════════════════════════════════════════════════════════════════════════
# QURAN TEXT
# ══════════════════════════════════════════════════════════════════════════════

def get_ayah_text(json_data, surah: int, ayah: int) -> str:
    """
    Extract text from JSON. Handles 3 formats:
      A. Flat list: [{"surah": 1, "ayah": 1, "text": "..."}]
      B. Nested:    {"1": {"1": "text"}}
      C. verse_key: [{"verse_key": "1:1", "text": "..."}]
    """
    if isinstance(json_data, list):
        for item in json_data:
            if item.get("surah") == surah and item.get("ayah") == ayah:
                return item.get("text", "")
            if item.get("verse_key") == f"{surah}:{ayah}":
                return item.get("text", "")
    elif isinstance(json_data, dict):
        return json_data.get(str(surah), {}).get(str(ayah), "")
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# SUBTITLES
# ══════════════════════════════════════════════════════════════════════════════

def sec_to_ass(s: float) -> str:
    h   = int(s // 3600)
    m   = int((s % 3600) // 60)
    sec = s % 60
    return f"{h}:{m:02d}:{sec:05.2f}"


def build_subtitles(
    batch:           list,
    arabic_data,
    english_data,
    audio_durations: list,
    out_path:        Path,
) -> None:
    """
    Write ASS subtitle file.
    Arabic  -> top center, large, Noto Naskh Arabic
    English -> bottom center, smaller, Noto Sans
    Each ayah shown for exactly its audio duration.
    """
    # Font sizes scaled up for 2K resolution
    header = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1440
PlayResY: 2560
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Arabic,Noto Naskh Arabic,105,&H00FFFFFF,&H000000FF,&H00000000,&HAA000000,1,0,0,0,100,100,2,0,1,5,3,8,80,80,120,1
Style: English,Noto Sans,58,&H00FFFAF0,&H000000FF,&H00000000,&HAA000000,0,0,0,0,100,100,0,0,1,4,2,2,80,80,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    cursor = 0.0

    for (surah, ayah), duration in zip(batch, audio_durations):
        start = sec_to_ass(cursor)
        end   = sec_to_ass(cursor + duration)

        ar_text = get_ayah_text(arabic_data, surah, ayah)
        en_text = get_ayah_text(english_data, surah, ayah)

        # Wrap English at 38 chars for clean mobile display at 2K
        if en_text:
            en_wrapped = r"\N".join(textwrap.wrap(en_text, width=38))
        else:
            en_wrapped = ""

        if ar_text:
            events.append(f"Dialogue: 0,{start},{end},Arabic,,0,0,0,,{ar_text}")
        if en_wrapped:
            events.append(f"Dialogue: 0,{start},{end},English,,0,0,0,,{en_wrapped}")

        cursor += duration

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
        f.write("\n")

    print(f"  Subtitles written -> {out_path.name}")


# ══════════════════════════════════════════════════════════════════════════════
# PEXELS BACKGROUND
# ══════════════════════════════════════════════════════════════════════════════

def fetch_background(min_duration: float) -> str:
    """Fetch a portrait nature video URL from Pexels."""
    headers = {"Authorization": PEXELS_API_KEY}
    query   = random.choice(NATURE_QUERIES)
    print(f"  Pexels query: '{query}'")

    r = requests.get(
        "https://api.pexels.com/videos/search",
        headers=headers,
        params={"query": query, "orientation": "portrait", "size": "large", "per_page": 20},
        timeout=30,
    )
    r.raise_for_status()
    videos = r.json().get("videos", [])

    if not videos:
        raise RuntimeError(f"Pexels returned no videos for '{query}'")

    # Filter by duration, prefer portrait files, pick highest resolution
    suitable = [v for v in videos if v.get("duration", 0) >= min_duration]
    pool     = suitable if suitable else sorted(videos, key=lambda v: v.get("duration", 0), reverse=True)

    for vid in pool:
        files    = vid.get("video_files", [])
        portrait = [f for f in files if f.get("height", 0) > f.get("width", 1)]
        best_set = portrait if portrait else files
        best_set.sort(key=lambda x: x.get("width", 0) * x.get("height", 0), reverse=True)
        url = best_set[0].get("link") if best_set else None
        if url:
            print(f"  Background selected ({vid.get('duration')}s): {vid.get('url')}")
            return url

    raise RuntimeError("No suitable Pexels video found.")


def download_background(url: str, dest: Path) -> None:
    print("  Downloading background video...")
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=512 * 1024):
            if chunk:
                f.write(chunk)
    print(f"  Background saved -> {dest.name}")


# ══════════════════════════════════════════════════════════════════════════════
# VIDEO MERGE
# ══════════════════════════════════════════════════════════════════════════════

def merge_video(
    bg_path:        Path,
    audio_path:     Path,
    subtitle_path:  Path,
    out_path:       Path,
    total_duration: float,
) -> None:
    """
    Merge background + audio + subtitles into final 2K 9:16 video.
    Audio is copied as-is (no quality loss).
    Video encoded at CRF 16 (very high quality).
    """
    # Escape path for FFmpeg ass filter on Linux
    sub_escaped = str(subtitle_path.resolve()).replace("\\", "/").replace(":", "\\:")

    vf = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
        f"ass={sub_escaped}"
    )

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-stream_loop", "-1",       # loop bg if shorter than audio
            "-i", str(bg_path),
            "-i", str(audio_path),
            "-t", str(total_duration),
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "slow",
            "-crf", "16",               # 16 = very high quality, near-lossless visually
            "-pix_fmt", "yuv420p",      # universal compatibility
            "-c:a", "copy",             # audio: no re-encode
            "-movflags", "+faststart",  # streaming optimised
            "-shortest",
            str(out_path),
        ],
        check=True,
    )
    print(f"  Video merged -> {out_path}")


# ══════════════════════════════════════════════════════════════════════════════
# METADATA
# ══════════════════════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    with tempfile.TemporaryDirectory() as _tmp:
        tmpdir = Path(_tmp)

        # ── Load Quran text ───────────────────────────────────────────────────
        print("Loading Quran text data...")
        with open(ARABIC_JSON, "r", encoding="utf-8") as f:
            arabic_data = json.load(f)
        with open(ENGLISH_JSON, "r", encoding="utf-8") as f:
            english_data = json.load(f)

        # ── Determine next batch ──────────────────────────────────────────────
        print("\nCalculating next batch...")
        batch, surah_name_en, surah_name_ar = next_batch()
        surah_num = batch[0][0]

        # ── Download audio ────────────────────────────────────────────────────
        print("\nDownloading ayah audio...")
        audio_files, audio_durations = download_all_audio(batch, tmpdir)
        total_duration = sum(audio_durations)
        print(f"  Total duration: {total_duration:.2f}s")

        # ── Concatenate audio ─────────────────────────────────────────────────
        print("\nConcatenating audio (no re-encode)...")
        combined_audio = tmpdir / "combined_audio.mp3"
        concat_audio(audio_files, combined_audio)

        # ── Build subtitles ───────────────────────────────────────────────────
        print("\nBuilding subtitles...")
        subtitle_file = tmpdir / "subtitles.ass"
        build_subtitles(batch, arabic_data, english_data, audio_durations, subtitle_file)

        # ── Fetch background ──────────────────────────────────────────────────
        print("\nFetching Pexels nature background...")
        bg_url  = fetch_background(min_duration=max(10, total_duration))
        bg_path = tmpdir / "background.mp4"
        download_background(bg_url, bg_path)

        # ── Merge final video ─────────────────────────────────────────────────
        print("\nMerging final video (2K quality)...")
        merge_video(bg_path, combined_audio, subtitle_file, OUTPUT_VIDEO, total_duration)

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

        # ── Save progress (LAST — only after full success) ────────────────────
        print("\nSaving progress...")
        save_progress(surah_num, batch[-1][1])

        print(f"\nDone!")
        print(f"  Video : {OUTPUT_VIDEO}")
        print(f"  Title : {title}")


if __name__ == "__main__":
    main()
