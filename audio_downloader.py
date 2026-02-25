#!/usr/bin/env python3
"""
audio_downloader.py
Downloads ayah audio from everyayah.com in strict Quran order.

Progress stored in TWO places for reliability:
  1. GitHub Repository Variable (QURAN_PROGRESS) via GitHub API — primary
  2. progress.json in repo — backup fallback

This dual approach means even if git commit fails, progress is saved
in the GitHub Variable and the next run reads correctly.
"""

import json
import os
import subprocess
import requests
from pathlib import Path

from surah_data import SURAHS

# ─── CONFIG ────────────────────────────────────────────────────────────────────
RECITER_FOLDER  = "Saood_ash-Shuraym_128kbps"
RECITER_NAME    = "Saud Al-Shuraim"
EVERYAYAH_BASE  = "https://everyayah.com/data"
AYAH_PER_VIDEO  = 7
SMALL_SURAH_MAX = 10
PROGRESS_FILE   = Path("progress.json")

# GitHub API settings — read from environment (set as secrets in repo)
GITHUB_TOKEN    = os.environ.get("GH_PAT", "")
GITHUB_REPO     = os.environ.get("GITHUB_REPOSITORY", "")  # auto-set by GitHub Actions
VARIABLE_NAME   = "QURAN_PROGRESS"
# ───────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
# GITHUB VARIABLE PROGRESS (primary — most reliable)
# ══════════════════════════════════════════════════════════════════════════════

def read_github_variable() -> dict | None:
    """Read QURAN_PROGRESS from GitHub Repository Variables via API."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return None
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/variables/{VARIABLE_NAME}"
        r   = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept":        "application/vnd.github+json",
            },
            timeout=10,
        )
        if r.status_code == 200:
            value = r.json().get("value", "")
            data  = json.loads(value)
            print(f"  [GitHub Variable] Progress: Surah {data.get('last_surah')}, Ayah {data.get('last_ayah')}")
            return data
        elif r.status_code == 404:
            print(f"  [GitHub Variable] {VARIABLE_NAME} not found yet. Will create on first save.")
            return None
        else:
            print(f"  [GitHub Variable] Read failed: HTTP {r.status_code}")
            return None
    except Exception as e:
        print(f"  [GitHub Variable] Read error: {e}")
        return None


def write_github_variable(surah: int, ayah: int) -> bool:
    """Write progress to GitHub Repository Variable via API."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("  [GitHub Variable] No token/repo — skipping variable write.")
        return False
    try:
        value   = json.dumps({"last_surah": surah, "last_ayah": ayah})
        url_get = f"https://api.github.com/repos/{GITHUB_REPO}/actions/variables/{VARIABLE_NAME}"
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept":        "application/vnd.github+json",
        }

        # Check if variable exists — use PATCH to update, POST to create
        check = requests.get(url_get, headers=headers, timeout=10)
        if check.status_code == 200:
            # Update existing
            r = requests.patch(
                url_get,
                headers=headers,
                json={"name": VARIABLE_NAME, "value": value},
                timeout=10,
            )
        else:
            # Create new
            url_create = f"https://api.github.com/repos/{GITHUB_REPO}/actions/variables"
            r = requests.post(
                url_create,
                headers=headers,
                json={"name": VARIABLE_NAME, "value": value},
                timeout=10,
            )

        if r.status_code in (200, 201, 204):
            print(f"  [GitHub Variable] Saved: Surah {surah}, Ayah {ayah}")
            return True
        else:
            print(f"  [GitHub Variable] Write failed: HTTP {r.status_code} — {r.text[:200]}")
            return False
    except Exception as e:
        print(f"  [GitHub Variable] Write error: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# FILE PROGRESS (backup fallback)
# ══════════════════════════════════════════════════════════════════════════════

def read_file_progress() -> dict:
    """Read progress from progress.json file (backup)."""
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE, "r") as f:
                data = json.load(f)
            data.pop("note", None)
            print(f"  [File] Progress: Surah {data.get('last_surah', 1)}, Ayah {data.get('last_ayah', 0)}")
            return data
        except Exception as e:
            print(f"  [File] Read error: {e}")
    print("  [File] No progress.json — starting from beginning.")
    return {"last_surah": 1, "last_ayah": 0}


def write_file_progress(surah: int, ayah: int) -> None:
    """Write progress to progress.json (backup)."""
    try:
        with open(PROGRESS_FILE, "w") as f:
            json.dump({"last_surah": surah, "last_ayah": ayah}, f, indent=2)
        print(f"  [File] Saved: Surah {surah}, Ayah {ayah}")
    except Exception as e:
        print(f"  [File] Write error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# UNIFIED LOAD / SAVE
# ══════════════════════════════════════════════════════════════════════════════

def load_progress() -> dict:
    """
    Load progress. GitHub Variable takes priority over file.
    If both exist, GitHub Variable wins (it's always more up to date).
    """
    print("Loading progress...")
    gh_progress   = read_github_variable()
    file_progress = read_file_progress()

    if gh_progress:
        # GitHub Variable is primary — always trust it over file
        return gh_progress
    return file_progress


def save_progress(surah: int, ayah: int) -> None:
    """
    Save progress to BOTH GitHub Variable and file.
    Called only after full pipeline success.
    """
    print(f"\nSaving progress: Surah {surah}, Ayah {ayah}...")
    write_github_variable(surah, ayah)
    write_file_progress(surah, ayah)


# ══════════════════════════════════════════════════════════════════════════════
# BATCH LOGIC
# ══════════════════════════════════════════════════════════════════════════════

def get_next_batch() -> tuple:
    """
    Get the next batch of ayahs in strict Quran order.
    Never repeats. Never skips. Auto-advances to next surah.
    """
    progress  = load_progress()
    cur_surah = progress.get("last_surah", 1)
    cur_ayah  = progress.get("last_ayah", 0)

    surah_map = {s[0]: s for s in SURAHS}

    # Validate surah number
    if cur_surah not in surah_map:
        print(f"  Invalid surah {cur_surah} in progress — resetting to Surah 1.")
        cur_surah, cur_ayah = 1, 0

    s_num, s_name_en, s_name_ar, total_ayah = surah_map[cur_surah]
    next_ayah = cur_ayah + 1

    # Advance to next surah if current is complete
    if next_ayah > total_ayah:
        next_surah_num = cur_surah + 1
        if next_surah_num > 114:
            print("  Full Quran complete — restarting from Surah 1.")
            next_surah_num = 1
        s_num, s_name_en, s_name_ar, total_ayah = surah_map[next_surah_num]
        next_ayah = 1
        print(f"  Surah {cur_surah} complete. Advancing to Surah {s_num} ({s_name_en}).")

    remaining  = total_ayah - next_ayah + 1
    batch_size = remaining if total_ayah <= SMALL_SURAH_MAX else min(AYAH_PER_VIDEO, remaining)
    batch      = [(s_num, a) for a in range(next_ayah, next_ayah + batch_size)]

    print(f"  Batch confirmed: Surah {s_num} ({s_name_en}), Ayahs {next_ayah}-{next_ayah + batch_size - 1}")
    return batch, s_name_en, s_name_ar


# ══════════════════════════════════════════════════════════════════════════════
# AUDIO DOWNLOAD
# ══════════════════════════════════════════════════════════════════════════════

def get_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    return float(r.stdout.strip())


def download_batch(batch: list, tmpdir: Path) -> tuple:
    """Download all ayahs in batch. Returns (audio_files, durations)."""
    audio_files, durations = [], []
    for surah, ayah in batch:
        url  = f"{EVERYAYAH_BASE}/{RECITER_FOLDER}/{surah:03d}{ayah:03d}.mp3"
        dest = tmpdir / f"ayah_{surah:03d}_{ayah:03d}.mp3"
        print(f"  Audio {surah}:{ayah} <- {url}")
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"everyayah.com returned HTTP {r.status_code} for {url}")
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
