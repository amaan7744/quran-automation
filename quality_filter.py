#!/usr/bin/env python3
"""
quality_filter.py
Scores and gates downloaded stock clips before they are allowed into
the edit. Uses ffprobe for stream metadata and ffmpeg analysis filters
(blurdetect, freezedetect, vidstabdetect) for perceptual quality —
no ML dependencies required.

Also owns a small on-disk cache keyed by clip URL/id so the same
footage is never re-downloaded or reused across runs.
"""

import hashlib
import json
import subprocess
from pathlib import Path

from config import (
    CACHE_DIR, CACHE_INDEX_FILE, MIN_CLIP_WIDTH, MIN_CLIP_HEIGHT,
    MIN_CLIP_FPS, MAX_ASPECT_DEVIATION, BLUR_SCORE_MIN, SHAKE_SCORE_MAX,
    MIN_VIDEO_BITRATE_KBPS,
)
from logging_utils import get_logger

log = get_logger(__name__)

TARGET_ASPECT = 9 / 16


# ══════════════════════════════════════════════════════════════════════════
# CACHE / DEDUPE
# ══════════════════════════════════════════════════════════════════════════

def _load_cache_index() -> dict:
    if CACHE_INDEX_FILE.exists():
        try:
            return json.loads(CACHE_INDEX_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("Cache index corrupted — starting fresh.")
    return {}


def _save_cache_index(index: dict) -> None:
    CACHE_INDEX_FILE.write_text(json.dumps(index, indent=2), encoding="utf-8")


def clip_id_for(pexels_video_id) -> str:
    return hashlib.sha1(str(pexels_video_id).encode()).hexdigest()[:16]


def is_used_before(pexels_video_id) -> bool:
    """True if this Pexels video id has already been used in a previous render."""
    index = _load_cache_index()
    cid = clip_id_for(pexels_video_id)
    return index.get(cid, {}).get("used", False)


def mark_used(pexels_video_id) -> None:
    index = _load_cache_index()
    cid = clip_id_for(pexels_video_id)
    entry = index.setdefault(cid, {})
    entry["used"] = True
    _save_cache_index(index)


def cached_path_for(pexels_video_id) -> Path:
    return CACHE_DIR / f"{clip_id_for(pexels_video_id)}.mp4"


def remember_download(pexels_video_id, local_path: Path, score: float) -> None:
    index = _load_cache_index()
    cid = clip_id_for(pexels_video_id)
    entry = index.setdefault(cid, {})
    entry.update({"path": str(local_path), "score": score, "used": entry.get("used", False)})
    _save_cache_index(index)


# ══════════════════════════════════════════════════════════════════════════
# FFPROBE METADATA
# ══════════════════════════════════════════════════════════════════════════

def probe(path: Path) -> dict:
    """Return width, height, fps, duration, bitrate for a local video file."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,bit_rate,duration",
        "-show_entries", "format=duration,bit_rate",
        "-of", "json", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {result.stderr[-300:]}")

    data = json.loads(result.stdout)
    stream = (data.get("streams") or [{}])[0]
    fmt = data.get("format", {})

    fps_raw = stream.get("r_frame_rate", "0/1")
    try:
        num, den = fps_raw.split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0

    bitrate = stream.get("bit_rate") or fmt.get("bit_rate") or 0
    duration = stream.get("duration") or fmt.get("duration") or 0

    return {
        "width": int(stream.get("width", 0)),
        "height": int(stream.get("height", 0)),
        "fps": fps,
        "bitrate_kbps": int(bitrate) // 1000 if bitrate else 0,
        "duration": float(duration) if duration else 0.0,
    }


# ══════════════════════════════════════════════════════════════════════════
# PERCEPTUAL ANALYSIS (blur / freeze / shake)
# ══════════════════════════════════════════════════════════════════════════

def measure_blur(path: Path) -> float:
    """
    Runs ffmpeg's blurdetect filter and returns the average blur score
    (0 = perfectly sharp, higher = blurrier). Analyzes at most 3s to stay fast.
    """
    cmd = [
        "ffmpeg", "-v", "info", "-t", "3", "-i", str(path),
        "-vf", "blurdetect", "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    scores = []
    for line in result.stderr.splitlines():
        if "blur:" in line:
            try:
                scores.append(float(line.split("blur:")[1].split()[0]))
            except (ValueError, IndexError):
                continue
    if not scores:
        return 0.0
    # blurdetect reports 0-1 (higher = blurrier); scale to a 0-100 style score
    return (sum(scores) / len(scores)) * 100


def measure_freeze(path: Path) -> bool:
    """True if the clip contains a frozen/static segment (dead stock footage)."""
    cmd = [
        "ffmpeg", "-v", "info", "-i", str(path),
        "-vf", "freezedetect=n=-30dB:d=1.0", "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return "freeze_start" in result.stderr


def measure_shake(path: Path, tmpdir: Path) -> float:
    """
    Runs vidstabdetect and reads the average per-frame transform magnitude
    out of the generated transforms file as a shakiness proxy.
    """
    trf = tmpdir / f"{path.stem}.trf"
    cmd = [
        "ffmpeg", "-v", "quiet", "-t", "3", "-i", str(path),
        "-vf", f"vidstabdetect=shakiness=6:result={trf}", "-f", "null", "-",
    ]
    subprocess.run(cmd, capture_output=True, text=True)
    if not trf.exists():
        return 0.0
    try:
        text = trf.read_text(errors="ignore")
        mags = []
        for line in text.splitlines():
            if line.startswith("Frame"):
                # format: Frame <n> {vec ...}  — approximate magnitude by field count/values
                nums = [float(x) for x in line.replace("{", " ").replace("}", " ").split()
                        if x.replace("-", "").replace(".", "").isdigit()]
                if len(nums) >= 3:
                    mags.append(abs(nums[1]) + abs(nums[2]))
        return sum(mags) / len(mags) if mags else 0.0
    except OSError:
        return 0.0
    finally:
        trf.unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════════════════
# COMBINED SCORE / GATE
# ══════════════════════════════════════════════════════════════════════════

def score_and_gate(path: Path, tmpdir: Path) -> tuple:
    """
    Runs all quality checks on a downloaded clip.
    Returns (passed: bool, score: float, reasons: list[str]).
    Higher score = better; only clips that pass every hard gate are scored.
    """
    reasons = []
    meta = probe(path)

    if meta["width"] < MIN_CLIP_WIDTH or meta["height"] < MIN_CLIP_HEIGHT:
        reasons.append(f"resolution too low ({meta['width']}x{meta['height']})")
    if meta["height"] <= meta["width"]:
        reasons.append("landscape orientation")
    else:
        aspect = meta["width"] / meta["height"]
        if abs(aspect - TARGET_ASPECT) > MAX_ASPECT_DEVIATION:
            reasons.append(f"aspect ratio too far from 9:16 ({aspect:.2f})")
    if meta["fps"] < MIN_CLIP_FPS:
        reasons.append(f"fps too low ({meta['fps']:.1f})")
    if meta["bitrate_kbps"] and meta["bitrate_kbps"] < MIN_VIDEO_BITRATE_KBPS:
        reasons.append(f"bitrate too low ({meta['bitrate_kbps']}kbps)")

    if reasons:
        return False, 0.0, reasons

    blur = measure_blur(path)
    if blur > BLUR_SCORE_MIN:
        reasons.append(f"too blurry (blur={blur:.1f})")

    if measure_freeze(path):
        reasons.append("contains frozen/static segment")

    shake = measure_shake(path, tmpdir)
    if shake > SHAKE_SCORE_MAX:
        reasons.append(f"too shaky (shake={shake:.1f})")

    if reasons:
        return False, 0.0, reasons

    # Composite score: reward resolution, framerate, bitrate; penalize blur+shake
    score = (
        (meta["width"] * meta["height"]) / 1_000_000  # megapixels
        + meta["fps"] / 30
        + (meta["bitrate_kbps"] or 3000) / 3000
        - blur / 10
        - shake / 10
    )
    return True, round(score, 3), []
