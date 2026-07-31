#!/usr/bin/env python3
"""
quality_filter.py
Scores and gates downloaded stock clips before they are allowed into
the edit. Uses ffprobe for stream metadata and ffmpeg analysis filters
(blurdetect, freezedetect, deshake) for perceptual quality —
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
    MIN_CLIP_FPS, FPS_TOLERANCE, MAX_ASPECT_DEVIATION, BLUR_SCORE_MIN,
    SHAKE_SCORE_MAX, MIN_VIDEO_BITRATE_KBPS,
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
    Shake proxy: runs ffmpeg's built-in `deshake` filter (block-matching
    stabilization, single pass, no external files) alongside the original
    frames, takes the per-pixel absolute difference between original and
    stabilized output, and averages the luma difference (signalstats YAVG)
    across frames. A stable/tripod clip barely changes under stabilization
    (low diff); a shaky clip needs large corrections (high diff).

    This replaces a previous implementation that hand-parsed vidstabdetect's
    `.trf` transforms file — that file only contains raw local-motion
    feature lists in the installed libvidstab version
    (`Frame n (List <count> [(LM ...)])`), not a simple per-frame global
    transform, so the old parser was extracting unrelated numbers (e.g. the
    feature count, which is always large) and reporting them as "shake".
    That produced large, meaningless scores for essentially every clip —
    including genuinely stable ones — which is why low-shake clips were
    still being rejected.

    Calibrated empirically: a static test clip averages ~3.5, a visibly
    shaky test clip (30px sinusoidal jitter) averages ~11.4. See
    SHAKE_SCORE_MAX in config.py.
    """
    cmd = [
        "ffmpeg", "-v", "quiet", "-t", "3", "-i", str(path),
        "-filter_complex",
        "[0:v]split[a][b];[b]deshake=edge=blank[stab];"
        "[a][stab]blend=all_mode=difference,signalstats,"
        "metadata=print:file=-:key=lavfi.signalstats.YAVG",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    vals = []
    for line in result.stdout.splitlines():
        if "YAVG=" in line:
            try:
                vals.append(float(line.split("YAVG=")[1].strip()))
            except (ValueError, IndexError):
                continue
    return sum(vals) / len(vals) if vals else 0.0


# ══════════════════════════════════════════════════════════════════════════
# COMBINED SCORE / GATE
# ══════════════════════════════════════════════════════════════════════════

def analyze_clip(path: Path, tmpdir: Path) -> dict | None:
    """
    Runs all quality checks on a downloaded clip and always computes a
    composite score when the file is readable — even if it fails one or
    more soft/hard gates. This lets callers do strict gating (see
    score_and_gate) AND rank rejected-but-usable clips for fallback mode
    without re-running analysis.

    Returns None only if the file is genuinely corrupted/unusable (ffprobe
    can't read it, or it has no valid video dimensions/duration) — the only
    condition that should ever remove a clip from consideration entirely.

    Returns a dict: {meta, blur, freeze, shake, score, passed, reasons}.
    """
    try:
        meta = probe(path)
    except RuntimeError as e:
        log.warning("Corrupted/unreadable clip %s: %s", path, e)
        return None
    if meta["width"] <= 0 or meta["height"] <= 0 or meta["duration"] <= 0:
        log.warning("Unusable clip %s: no valid video stream (%s)", path, meta)
        return None

    reasons = []
    if meta["width"] < MIN_CLIP_WIDTH or meta["height"] < MIN_CLIP_HEIGHT:
        reasons.append(f"resolution too low ({meta['width']}x{meta['height']})")
    if meta["height"] <= meta["width"]:
        reasons.append("landscape orientation")
    else:
        aspect = meta["width"] / meta["height"]
        if abs(aspect - TARGET_ASPECT) > MAX_ASPECT_DEVIATION:
            reasons.append(f"aspect ratio too far from 9:16 ({aspect:.2f})")
    # FPS_TOLERANCE absorbs common NTSC-derived rates like 23.976 (24000/1001)
    # that are practically "24fps" but were being rejected by a strict '<'
    # comparison against an integer 24.
    if meta["fps"] < MIN_CLIP_FPS - FPS_TOLERANCE:
        reasons.append(f"fps too low ({meta['fps']:.2f})")
    if meta["bitrate_kbps"] and meta["bitrate_kbps"] < MIN_VIDEO_BITRATE_KBPS:
        reasons.append(f"bitrate too low ({meta['bitrate_kbps']}kbps)")

    blur = measure_blur(path)
    if blur > BLUR_SCORE_MIN:
        reasons.append(f"too blurry (blur={blur:.1f})")

    freeze = measure_freeze(path)
    if freeze:
        reasons.append("contains frozen/static segment")

    shake = measure_shake(path, tmpdir)
    if shake > SHAKE_SCORE_MAX:
        reasons.append(f"too shaky (shake={shake:.1f})")

    # Composite score: reward resolution, framerate, bitrate; penalize
    # blur+shake. Computed unconditionally (not just on pass) so fallback
    # ranking has a real quality signal instead of a flat 0.
    score = (
        (meta["width"] * meta["height"]) / 1_000_000  # megapixels
        + meta["fps"] / 30
        + (meta["bitrate_kbps"] or 3000) / 3000
        - blur / 10
        - shake / 10
    )
    return {
        "meta": meta, "blur": blur, "freeze": freeze, "shake": shake,
        "score": round(score, 3), "passed": not reasons, "reasons": reasons,
    }


def score_and_gate(path: Path, tmpdir: Path) -> tuple:
    """
    Strict pass/fail gate, kept for backward compatibility. Returns
    (passed: bool, score: float, reasons: list[str]) — only passing clips
    get a nonzero score. For fallback ranking of rejected-but-usable clips,
    call analyze_clip() directly instead.
    """
    result = analyze_clip(path, tmpdir)
    if result is None:
        return False, 0.0, ["corrupted or unreadable"]
    if not result["passed"]:
        return False, 0.0, result["reasons"]
    return True, result["score"], []
