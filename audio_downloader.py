#!/usr/bin/env python3
"""
audio_downloader.py
Pipeline Step 1 — Downloads ayah audio from everyayah.com.

- Reads progress.json to find next batch
- Downloads each ayah as individual MP3
- Concatenates into combined_audio.mp3 (no re-encode)
- Writes batch_info.json for subtitle_builder and pexels_fetcher to use
"""

import json
import subprocess
import requests
from pathlib import Path

from surah_data import SURAHS

RECITER_FOLDER  = "Saood_ash-Shuraym_128kbps"
RECITER_NAME    = "Saad Al-Ghamdi"
EVERYAYAH_BASE  = "https://everyayah.com/data"
AYAH_PER_VIDEO  = 7
SMALL_SURAH_MAX = 10

PROGRESS_FILE   = Path("progress.json")
AUDIO_DIR       = Path("audio_segments")
OUTPUT_AUDIO    = Path("combined_audio.mp3")
BATCH_INFO_FILE = Path("batch_info.json")


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            data = json.load(f)
        data.pop("note", None)
        return data
    return {"last_surah": 1, "last_ayah": 0}


def next_batch() -> tuple:
    progress  = load_progress()
    cur_surah = progress["last_surah"]
    cur_ayah  = progress["last_ayah"]
    surah_map = {s[0]: s for s in SURAHS}

    s_num, s_name_en, s_name_ar, total_ayah = surah_map[cur_surah]
    next_ayah = cur_ayah + 1

    if next_ayah > total_ayah:
        nxt = cur_surah + 1
        if nxt > 114:
            print("  Quran complete — restarting from Surah 1.")
            nxt = 1
        s_num, s_name_en, s_name_ar, total_ayah = surah_map[nxt]
        next_ayah = 1
        print(f"  Surah {cur_surah} done. Moving to Surah {s_num} ({s_name_en}).")

    remaining  = total_ayah - next_ayah + 1
    batch_size = remaining if total_ayah <= SMALL_SURAH_MAX else min(AYAH_PER_VIDEO, remaining)
    batch      = [(s_num, a) for a in range(next_ayah, next_ayah + batch_size)]
    print(f"  Batch -> Surah {s_num} ({s_name_en}), Ayahs {next_ayah}–{next_ayah + batch_size - 1}")
    return batch, s_name_en, s_name_ar


def get_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    return float(r.stdout.strip())


def download_ayah(surah: int, ayah: int, dest: Path) -> None:
    url = f"{EVERYAYAH_BASE}/{RECITER_FOLDER}/{surah:03d}{ayah:03d}.mp3"
    print(f"  [{surah}:{ayah}] {url}")
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} for {url}")
    dest.write_bytes(r.content)


def concat_audios(files: list, out: Path) -> None:
    lst = out.parent / "audio_list.txt"
    with open(lst, "w") as f:
        for p in files:
            f.write(f"file '{Path(p).resolve()}'\n")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c", "copy", str(out)],
        check=True, capture_output=True,
    )


def main():
    print("=" * 40)
    print("STEP 1 — AUDIO DOWNLOADER")
    print("=" * 40)
    AUDIO_DIR.mkdir(exist_ok=True)

    print("\nFinding next batch...")
    batch, surah_name_en, surah_name_ar = next_batch()
    surah_num = batch[0][0]

    print(f"\nDownloading {len(batch)} ayahs from everyayah.com...")
    audio_files     = []
    audio_durations = []

    for surah, ayah in batch:
        dest = AUDIO_DIR / f"{surah:03d}_{ayah:03d}.mp3"
        download_ayah(surah, ayah, dest)
        dur = get_duration(dest)
        print(f"    Duration: {dur:.2f}s")
        audio_files.append(str(dest))
        audio_durations.append(dur)

    total = sum(audio_durations)
    print(f"\n  Total audio: {total:.2f}s across {len(batch)} ayahs")

    print("\nConcatenating (no re-encode)...")
    concat_audios(audio_files, OUTPUT_AUDIO)
    print(f"  Done -> {OUTPUT_AUDIO}")

    batch_info = {
        "surah_num":       surah_num,
        "surah_name_en":   surah_name_en,
        "surah_name_ar":   surah_name_ar,
        "batch":           batch,
        "audio_files":     audio_files,
        "audio_durations": audio_durations,
        "total_duration":  total,
        "combined_audio":  str(OUTPUT_AUDIO),
    }
    with open(BATCH_INFO_FILE, "w", encoding="utf-8") as f:
        json.dump(batch_info, f, ensure_ascii=False, indent=2)
    print(f"  Batch info -> {BATCH_INFO_FILE}")


if __name__ == "__main__":
    main()
