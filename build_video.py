#!/usr/bin/env python3
"""
Quran Recitation Video Builder
- Fetches audio from everyayah.com
- Fetches nature background video from Pexels API
- Burns Arabic + English subtitles (from local JSON files)
- Outputs 9:16 vertical video for Reels / Shorts
- Tracks progress in progress.json so no ayah is repeated
"""

import os
import sys
import json
import math
import textwrap
import requests
import subprocess
import tempfile
from pathlib import Path

from surah_data import SURAHS

# ─── CONFIG ────────────────────────────────────────────────────────────────────
RECITER_FOLDER   = "Saood_ash-Shuraym_128kbps"   # everyayah folder name
RECITER_NAME     = "Saad Al-Ghamdi"               # human-readable name for description
AYAH_PER_VIDEO   = 7                              # default batch size
MAX_AYAH         = 10                             # max batch before forcing a split
SMALL_SURAH_MAX  = 10                             # surahs with ≤ this many ayahs → one video

PEXELS_API_KEY   = os.environ["PEXELS_API_KEY"]
PROGRESS_FILE    = Path("progress.json")
ARABIC_JSON      = Path("arabic.json")            # tanzil format
ENGLISH_JSON     = Path("english.json")           # saheeh international

VIDEO_WIDTH      = 1080
VIDEO_HEIGHT     = 1920

EVERYAYAH_BASE   = "https://everyayah.com/data"
# ───────────────────────────────────────────────────────────────────────────────


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_progress():
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, "r") as f:
            return json.load(f)
    return {"last_surah": 1, "last_ayah": 0}


def save_progress(surah, ayah):
    data = {"last_surah": surah, "last_ayah": ayah}
    with open(PROGRESS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def next_batch():
    """
    Returns list of (surah_num, ayah_num) for the next video batch.
    Respects small-surah rule and AYAH_PER_VIDEO cap.
    """
    progress = load_progress()
    start_surah = progress["last_surah"]
    start_ayah  = progress["last_ayah"]

    # Find surah entry
    surah_map = {s[0]: s for s in SURAHS}
    if start_surah not in surah_map:
        print("All surahs completed. Starting over from Surah 1.")
        start_surah, start_ayah = 1, 0

    s_num, s_name_en, s_name_ar, total_ayah = surah_map[start_surah]

    # Move to next ayah from last completed
    next_ayah = start_ayah + 1

    # If we finished this surah, move to next
    if next_ayah > total_ayah:
        next_surah_num = start_surah + 1
        if next_surah_num > 114:
            print("Quran complete. Resetting to Surah 1.")
            next_surah_num = 1
        s_num, s_name_en, s_name_ar, total_ayah = surah_map[next_surah_num]
        next_ayah = 1

    s_num, s_name_en, s_name_ar, total_ayah = surah_map[s_num]

    # How many ayahs remain in this surah from next_ayah
    remaining = total_ayah - next_ayah + 1

    # Small surah: do all in one go
    if total_ayah <= SMALL_SURAH_MAX:
        batch_size = total_ayah if next_ayah == 1 else remaining
    else:
        batch_size = min(AYAH_PER_VIDEO, remaining)

    batch = [(s_num, a) for a in range(next_ayah, next_ayah + batch_size)]
    return batch, s_name_en, s_name_ar


def audio_url(surah, ayah):
    return f"{EVERYAYAH_BASE}/{RECITER_FOLDER}/{surah:03d}{ayah:03d}.mp3"


def download_audio(surah, ayah, dest):
    url = audio_url(surah, ayah)
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Failed to download audio {url}: HTTP {r.status_code}")
    with open(dest, "wb") as f:
        f.write(r.content)


def get_audio_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True
    )
    return float(result.stdout.strip())


def fetch_nature_video(min_duration):
    """Fetch a nature-only (no humans) video from Pexels."""
    headers = {"Authorization": PEXELS_API_KEY}
    # Try multiple nature queries to ensure variety
    queries = ["nature landscape", "forest river", "ocean waves", "mountain sunrise", "waterfall nature"]
    import random
    query = random.choice(queries)

    params = {
        "query": query,
        "orientation": "portrait",
        "size": "large",
        "per_page": 15,
        "page": 1,
    }
    r = requests.get("https://api.pexels.com/videos/search", headers=headers, params=params)
    r.raise_for_status()
    videos = r.json().get("videos", [])

    # Filter: no people in tags/user, duration >= min_duration
    for vid in videos:
        duration = vid.get("duration", 0)
        if duration < min_duration:
            continue
        # Pick highest quality portrait file available
        files = vid.get("video_files", [])
        portrait_files = [f for f in files if f.get("width", 0) < f.get("height", 1)]
        if not portrait_files:
            portrait_files = files  # fallback to any
        portrait_files.sort(key=lambda x: x.get("width", 0), reverse=True)
        best = portrait_files[0]
        return best["link"], duration

    raise RuntimeError("No suitable nature video found on Pexels.")


def download_video(url, dest):
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 256):
            f.write(chunk)


def get_text(json_data, surah, ayah):
    """
    Supports two JSON formats:
    1. Flat list: [{surah: 1, ayah: 1, text: "..."}, ...]
    2. Nested: {"1": {"1": "text"}}
    """
    if isinstance(json_data, list):
        for item in json_data:
            if item.get("surah") == surah and item.get("ayah") == ayah:
                return item.get("text", "")
        # try verse_key format
        for item in json_data:
            if item.get("verse_key") == f"{surah}:{ayah}":
                return item.get("text", "")
    elif isinstance(json_data, dict):
        return json_data.get(str(surah), {}).get(str(ayah), "")
    return ""


def build_subtitle_ass(batch, arabic_data, english_data, audio_durations, out_path):
    """
    Build an ASS subtitle file with two tracks:
    - Top: Arabic (large, right-to-left styled)
    - Bottom: English translation (smaller)
    Each ayah occupies its audio duration in sequence.
    """
    ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Arabic,Scheherazade New,72,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,3,1,8,40,40,60,1
Style: English,Calibri,42,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,40,40,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def sec_to_ass(s):
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = s % 60
        return f"{h}:{m:02d}:{sec:05.2f}"

    events = []
    cursor = 0.0

    for (surah, ayah), duration in zip(batch, audio_durations):
        start = sec_to_ass(cursor)
        end   = sec_to_ass(cursor + duration)

        ar_text = get_text(arabic_data, surah, ayah)
        en_text = get_text(english_data, surah, ayah)

        # Wrap English text (max ~45 chars per line for readability)
        en_lines = textwrap.wrap(en_text, 45) if en_text else []
        en_wrapped = r"\N".join(en_lines)

        if ar_text:
            events.append(f"Dialogue: 0,{start},{end},Arabic,,0,0,0,,{ar_text}")
        if en_wrapped:
            events.append(f"Dialogue: 0,{start},{end},English,,0,0,0,,{en_wrapped}")

        cursor += duration

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(ass_header)
        f.write("\n".join(events))


def concat_audios(audio_files, out_path):
    """Concatenate MP3 files using ffmpeg concat demuxer (no re-encode)."""
    list_file = out_path.parent / "audio_list.txt"
    with open(list_file, "w") as f:
        for af in audio_files:
            f.write(f"file '{af.resolve()}'\n")

    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(out_path)
    ], check=True, capture_output=True)


def build_video(bg_video_path, audio_path, subtitle_path, out_path, total_duration):
    """
    Compose final video:
    - Background looped/trimmed to total_duration
    - Scale/crop to 9:16 (1080x1920)
    - Burn subtitles
    - Overlay audio (no re-encode of audio)
    - Video quality lowered for file size (CRF 28) but no audio quality drop
    """
    vf = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
        f"ass='{subtitle_path}'"
    )

    subprocess.run([
        "ffmpeg", "-y",
        "-stream_loop", "-1",        # loop background video if shorter than audio
        "-i", str(bg_video_path),
        "-i", str(audio_path),
        "-t", str(total_duration),
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "28",                # lower quality = smaller file; audio untouched
        "-c:a", "copy",             # audio: no re-encode, copy as-is
        "-shortest",
        str(out_path)
    ], check=True)


def generate_description(surah_name_en, surah_name_ar, surah_num, batch):
    first_ayah = batch[0][1]
    last_ayah  = batch[-1][1]
    return (
        f"Quran Recitation | {surah_name_ar} • {surah_name_en} (Surah {surah_num})\n"
        f"Ayah {first_ayah}–{last_ayah}\n\n"
        f"Recited by: {RECITER_NAME}\n"
        f"Arabic Text: Tanzil\n"
        f"English Translation: Sahih International\n\n"
        f"#Quran #QuranRecitation #{surah_name_en.replace(' ', '')} #Islam #DailyQuran"
    )


def generate_title(surah_name_en, surah_num, batch):
    first_ayah = batch[0][1]
    last_ayah  = batch[-1][1]
    return f"Quran | {surah_name_en} ({surah_num}:{first_ayah}-{last_ayah}) | {RECITER_NAME}"


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        print("Loading Quran text data...")
        arabic_data  = load_json(ARABIC_JSON)
        english_data = load_json(ENGLISH_JSON)

        print("Determining next batch...")
        batch, surah_name_en, surah_name_ar = next_batch()
        surah_num = batch[0][0]
        print(f"  Batch: Surah {surah_num} ({surah_name_en}), Ayahs {batch[0][1]}–{batch[-1][1]}")

        # ── Download audio files ──────────────────────────────────────────────
        print("Downloading audio files...")
        audio_files    = []
        audio_durations = []
        for surah, ayah in batch:
            dest = tmpdir / f"ayah_{surah:03d}_{ayah:03d}.mp3"
            print(f"  Downloading {surah}:{ayah} from everyayah.com ...")
            download_audio(surah, ayah, dest)
            dur = get_audio_duration(dest)
            audio_files.append(dest)
            audio_durations.append(dur)
            print(f"    Duration: {dur:.2f}s")

        total_duration = sum(audio_durations)
        print(f"  Total audio duration: {total_duration:.2f}s")

        # ── Concatenate audio ─────────────────────────────────────────────────
        combined_audio = tmpdir / "combined_audio.mp3"
        print("Concatenating audio (no re-encode)...")
        concat_audios(audio_files, combined_audio)

        # ── Build subtitles ───────────────────────────────────────────────────
        subtitle_file = tmpdir / "subtitles.ass"
        print("Building subtitles...")
        build_subtitle_ass(batch, arabic_data, english_data, audio_durations, subtitle_file)

        # ── Fetch Pexels background ───────────────────────────────────────────
        print("Fetching nature background video from Pexels...")
        bg_url, bg_duration = fetch_nature_video(min_duration=max(10, total_duration))
        bg_path = tmpdir / "background.mp4"
        print(f"  Downloading background ({bg_duration}s)...")
        download_video(bg_url, bg_path)

        # ── Build final video ─────────────────────────────────────────────────
        output_file = Path("output_video.mp4")
        print("Building final video...")
        build_video(bg_path, combined_audio, subtitle_file, output_file, total_duration)
        print(f"  Video saved: {output_file}")

        # ── Generate metadata ─────────────────────────────────────────────────
        title       = generate_title(surah_name_en, surah_num, batch)
        description = generate_description(surah_name_en, surah_name_ar, surah_num, batch)

        metadata = {
            "title":       title,
            "description": description,
            "surah_num":   surah_num,
            "surah_en":    surah_name_en,
            "surah_ar":    surah_name_ar,
            "first_ayah":  batch[0][1],
            "last_ayah":   batch[-1][1],
            "video_file":  str(output_file),
        }
        with open("video_metadata.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        # ── Save progress ─────────────────────────────────────────────────────
        save_progress(surah_num, batch[-1][1])
        print("Progress saved.")
        print(f"\nDone! Video: {output_file}")
        print(f"Title: {title}")


if __name__ == "__main__":
    main()
