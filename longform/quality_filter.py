#!/usr/bin/env python3
"""
quality_filter.py
Lightweight clip validation and history caching using ffprobe.

Deliberately simple: one ffprobe call per clip, no perceptual analysis
(no blur/freeze/shake detection, no scoring). A clip is rejected only if
it's unreadable, not portrait, below the minimum resolution, or shorter
than the minimum duration.
"""

import json
import subprocess
from pathlib import Path
from typing import Tuple

from longform_config import (
    CACHE_DIR,
    CACHE_INDEX_FILE,
    MIN_CLIP_DURATION,
    MIN_CLIP_HEIGHT,
    MIN_CLIP_WIDTH,
)
from logging_utils import get_logger

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════
# CACHE HELPERS
# ══════════════════════════════════════════════════════════════════════════

def clip_id_for(identifier: str | int) -> str:
    """Standardizes a clip identifier into a string format."""
    return str(identifier)


def cached_path_for(clip_id: str | int, cache_dir: Path = CACHE_DIR) -> Path:
    """Returns the expected file path for a given cached clip ID.

    `cache_dir` defaults to the Shorts/Reels CACHE_DIR; the long-form
    pipeline passes its own LONGFORM_CACHE_DIR so the two never collide or
    evict each other's downloads.
    """
    return cache_dir / f"pexels_{clip_id}.mp4"


def _load_index(index_file: Path = CACHE_INDEX_FILE) -> set[str]:
    """Loads the set of previously used clip IDs from disk."""
    if not index_file.exists():
        return set()
    try:
        with open(index_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return set(map(str, data))
    except Exception as e:
        log.warning("Failed to load cache index: %s", e)
    return set()


def is_used_before(clip_id: str | int, index_file: Path = CACHE_INDEX_FILE) -> bool:
    """Checks if the given clip ID has been used before."""
    cid = clip_id_for(clip_id)
    return cid in _load_index(index_file)


def mark_used(clip_id: str | int, index_file: Path = CACHE_INDEX_FILE) -> None:
    """Marks a clip ID as used in the cache index file."""
    cid = clip_id_for(clip_id)
    used = _load_index(index_file)
    if cid not in used:
        used.add(cid)
        try:
            index_file.parent.mkdir(parents=True, exist_ok=True)
            with open(index_file, "w", encoding="utf-8") as f:
                json.dump(sorted(list(used)), f, indent=2)
        except Exception as e:
            log.warning("Failed to save cache index: %s", e)


# ══════════════════════════════════════════════════════════════════════════
# VALIDATION
# ══════════════════════════════════════════════════════════════════════════

def validate_clip(
    path: Path,
    min_width: int = MIN_CLIP_WIDTH,
    min_height: int = MIN_CLIP_HEIGHT,
    min_duration: float = MIN_CLIP_DURATION,
    orientation: str = "portrait",
) -> Tuple[bool, str]:
    """
    Validates a video file using a single ffprobe call.
    Checks:
      - File readability and presence of video stream
      - Width >= min_width
      - Height >= min_height
      - Orientation ("portrait": height > width, "landscape": width > height,
        or "any" to skip the check) — defaults preserve the original
        Shorts/Reels portrait-only behavior for every existing caller.
      - Duration >= min_duration

    The four thresholds default to the module-level Shorts/Reels constants
    so every existing call site (pexels_fetcher.py's `_evaluate_candidate`)
    is unaffected; the long-form pipeline passes its own (larger,
    landscape) thresholds explicitly.
    """
    if not path.exists() or not path.is_file():
        return False, f"File does not exist or is not readable: {path}"

    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration:format=duration",
        "-of", "json",
        str(path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        return False, f"Corrupted file or ffprobe failed: {e}"

    streams = data.get("streams", [])
    if not streams:
        return False, "No video stream found"

    stream = streams[0]
    width = stream.get("width", 0)
    height = stream.get("height", 0)

    # Duration can be present in stream or format container
    duration_str = stream.get("duration") or data.get("format", {}).get("duration")
    try:
        duration = float(duration_str) if duration_str is not None else 0.0
    except ValueError:
        duration = 0.0

    if width < min_width:
        return False, f"Width {width} below minimum {min_width}"

    if height < min_height:
        return False, f"Height {height} below minimum {min_height}"

    if orientation == "portrait" and height <= width:
        return False, f"Not portrait orientation ({width}x{height})"
    if orientation == "landscape" and width <= height:
        return False, f"Not landscape orientation ({width}x{height})"

    if duration < min_duration:
        return False, f"Duration {duration:.2f}s below minimum {min_duration}s"

    return True, ""
