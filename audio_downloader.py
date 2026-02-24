#!/usr/bin/env python3
"""
audio_downloader.py
Downloads ayah audio from everyayah.com in strict Quran order.
No ayah is ever repeated or skipped.
Progress saved ONLY after full pipeline success.
"""

import json
import subprocess
import requests
from pathlib import Path

from surah_data import SURAHS

# ─── CONFIG ────────────────────────────────────────────────────────────────────
RECITER_FOLDER  = "Saood_ash-Shuraym_128kbps"
RECITER_NAME    = "Saad Al-Ghamdi"
EVERYAYAH_BASE  = "https://everyayah.com/data"
AYAH_PER_VIDEO  = 7
SMALL_SURAH_MAX = 10
PROGRESS_FILE   = Path("progress.json")
# ───────────────────────────────────────────────────────────────────────────────


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, "r") as f:
            data = json.load(f)
        data.pop("note", None)
        return data
    return {"last_surah": 1, "last_ayah": 0}


def save_progress(surah: int, ayah: int) -> None:
    """Call ONLY after full pipeline success to prevent skipping ayahs."""
    with open(PROGRESS_FILE, "w") as f:
        json.dump({"last_surah": surah, "last_ayah": ayah}, f, indent=2)
    print(f"  Progress saved -> Surah {surah}, Ayah {ayah}")


def get_next_batch() -> tuple:
    """
    Calculates the next batch of ayahs in strict order.
    Automatically advances to next surah when current is complete.
    Returns (batch, surah_name_en, surah_name_ar).
    """
    progress  = load_progress()
    cur_surah = progress["last_surah"]
    cur_ayah  = progress["last_ayah"]

    surah_map = {s[0]: s for s in SURAHS}
    s_num, s_name_en, s_name_ar, total_ayah = surah_map[cur_surah]
    next_ayah = cur_ayah + 1

    # Surah finished — move to next
    if next_ayah > total_ayah:
        next_surah_num = cur_surah + 1
        if next_surah_num > 114:
            print("  Full Quran complete. Restarting from Surah 1.")
            next_surah_num = 1
        s_num, s_name_en, s_name_ar, total_ayah = surah_map[next_surah_num]
        next_ayah = 1
        print(f"  Surah {cur_surah} done. Moving to Surah {s_num} ({s_name_en}).")

    remaining  = total_ayah - next_ayah + 1
    batch_size = remaining if total_ayah <= SMALL_SURAH_MAX else min(AYAH_PER_VIDEO, remaining)
    batch      = [(s_num, a) for a in range(next_ayah, next_ayah + batch_size)]

    print(f"  Batch: Surah {s_num} ({s_name_en}), Ayahs {next_ayah}-{next_ayah + batch_size - 1}")
    return batch, s_name_en, s_name_ar


def get_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    return float(r.stdout.strip())


def download_batch(batch: list, tmpdir: Path) -> tuple:
    """Download all ayahs. Returns (audio_files, durations)."""
    audio_files, durations = [], []
    for surah, ayah in batch:
        url  = f"{EVERYAYAH_BASE}/{RECITER_FOLDER}/{surah:03d}{ayah:03d}.mp3"
        dest = tmpdir / f"ayah_{surah:03d}_{ayah:03d}.mp3"
        print(f"  Audio {surah}:{ayah} <- {url}")
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} for {url}")
        dest.write_bytes(r.content)
        dur = get_duration(dest)
        print(f"    {dur:.2f}s")
        audio_files.append(dest)
        durations.append(dur)
    return audio_files, durations


def concat_audio(audio_files: list, out_path: Path) -> None:
    """Concatenate MP3s with zero quality loss — stream copy only."""
    list_file = out_path.parent / "audio_list.txt"
    with open(list_file, "w") as f:
        for af in audio_files:
            f.write(f"file '{af.resolve()}'\n")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", str(list_file), "-c", "copy", str(out_path)],
        check=True, capture_output=True,
    )
    print(f"  Audio ready -> {out_path.name}")
