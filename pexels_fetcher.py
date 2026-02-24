#!/usr/bin/env python3
"""
pexels_fetcher.py
Fetches multiple short nature video clips from Pexels (5-15 sec each)
and concatenates them to match the exact audio duration.
Every run uses different clips — never the same fixed video.
No humans — nature only.
"""

import os
import random
import subprocess
import requests
from pathlib import Path

PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]

# Wide variety of nature queries — shuffled each run for maximum variety
NATURE_QUERIES = [
    "waterfall nature",
    "ocean waves",
    "forest sunlight",
    "mountain river",
    "green forest",
    "desert dunes",
    "lake reflection",
    "rain drops nature",
    "snow mountain",
    "autumn leaves",
    "tropical beach waves",
    "misty forest",
    "sunrise nature",
    "clouds timelapse",
    "bamboo forest",
    "flowing stream",
    "meadow flowers",
    "rocky coastline",
    "pine forest",
    "volcano nature",
]


def search_pexels_clips(query: str, count: int = 5) -> list:
    """
    Search Pexels for short portrait nature clips.
    Returns list of video file URLs.
    """
    headers = {"Authorization": PEXELS_API_KEY}
    # Use random page to get different results each time
    page = random.randint(1, 5)

    r = requests.get(
        "https://api.pexels.com/videos/search",
        headers=headers,
        params={
            "query":       query,
            "orientation": "portrait",
            "size":        "medium",
            "per_page":    20,
            "page":        page,
        },
        timeout=30,
    )
    r.raise_for_status()
    videos = r.json().get("videos", [])

    clips = []
    for vid in videos:
        duration = vid.get("duration", 0)
        # Prefer clips between 5-20 seconds for variety
        if duration < 4 or duration > 25:
            continue

        files = vid.get("video_files", [])
        # Prefer portrait orientation
        portrait = [f for f in files if f.get("height", 0) > f.get("width", 1)]
        pool     = portrait if portrait else files
        # Highest resolution available
        pool.sort(key=lambda x: x.get("width", 0) * x.get("height", 0), reverse=True)

        if pool and pool[0].get("link"):
            clips.append({
                "url":      pool[0]["link"],
                "duration": duration,
                "width":    pool[0].get("width", 0),
                "height":   pool[0].get("height", 0),
            })

        if len(clips) >= count:
            break

    return clips


def fetch_clips_for_duration(total_duration: float) -> list:
    """
    Fetch enough short clips to cover total_duration seconds.
    Uses multiple random queries for maximum variety.
    Returns list of clip URLs.
    """
    queries  = random.sample(NATURE_QUERIES, k=min(6, len(NATURE_QUERIES)))
    all_clips = []

    for query in queries:
        print(f"  Searching Pexels: '{query}'")
        clips = search_pexels_clips(query, count=4)
        all_clips.extend(clips)
        if not clips:
            print(f"    No clips found for '{query}', trying next query.")

    if not all_clips:
        raise RuntimeError("Pexels returned no suitable clips. Check API key and quota.")

    # Shuffle all found clips for randomness
    random.shuffle(all_clips)

    # Pick clips until we have enough duration (with some buffer)
    selected    = []
    accumulated = 0.0
    needed      = total_duration * 1.3  # 30% buffer so we never run short

    # Cycle through clips if needed
    pool = all_clips * 10  # repeat pool to ensure enough duration
    for clip in pool:
        selected.append(clip)
        accumulated += clip["duration"]
        if accumulated >= needed:
            break

    print(f"  Selected {len(selected)} clips covering {accumulated:.1f}s for {total_duration:.1f}s audio")
    return selected


def download_clip(url: str, dest: Path) -> None:
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=512 * 1024):
            if chunk:
                f.write(chunk)


def download_and_concat_clips(clips: list, tmpdir: Path, out_path: Path, total_duration: float) -> None:
    """
    Download all clips, normalize them to same resolution and fps,
    concatenate into one seamless background video trimmed to total_duration.
    """
    clip_paths = []

    for i, clip in enumerate(clips):
        raw_path  = tmpdir / f"clip_raw_{i:03d}.mp4"
        norm_path = tmpdir / f"clip_norm_{i:03d}.mp4"

        print(f"  Downloading clip {i+1}/{len(clips)} ({clip['duration']}s)...")
        download_clip(clip["url"], raw_path)

        # Normalize each clip: scale to 1440x2560, same fps (30), same codec
        # This ensures seamless concatenation
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(raw_path),
                "-vf", "scale=1440:2560:force_original_aspect_ratio=increase,crop=1440:2560,fps=30",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "20",
                "-an",  # remove audio from background clips
                str(norm_path),
            ],
            check=True, capture_output=True,
        )
        clip_paths.append(norm_path)

    # Concatenate all normalized clips
    list_file = tmpdir / "clips_list.txt"
    with open(list_file, "w") as f:
        for cp in clip_paths:
            f.write(f"file '{cp.resolve()}'\n")

    concat_path = tmpdir / "bg_concat.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", str(list_file), "-c", "copy", str(concat_path)],
        check=True, capture_output=True,
    )

    # Trim exactly to total_duration
    subprocess.run(
        ["ffmpeg", "-y",
         "-i", str(concat_path),
         "-t", str(total_duration),
         "-c", "copy",
         str(out_path)],
        check=True, capture_output=True,
    )
    print(f"  Background ready -> {out_path.name} ({total_duration:.1f}s)")
