#!/usr/bin/env python3
"""
pexels_fetcher.py
Fetches, quality-gates, caches, and edits nature stock footage into a
single moving background video that matches the audio duration exactly.

NOTE ON SOURCE: this pipeline uses the Pexels video API. Pinterest has no
public API for searching/downloading third-party video content, so there
is no "Pinterest pipeline" to swap in here — this module is the real
visual source described in the brief, hardened with quality scoring,
caching, and motion editing.
"""

import random
import subprocess
from pathlib import Path

import requests

from config import (
    PEXELS_API_KEY, PEXELS_SEARCH_URL, NATURE_QUERIES, CLIPS_PER_QUERY,
    QUERIES_PER_RUN, DURATION_BUFFER, MIN_CLIP_DURATION, MAX_CLIP_DURATION,
    VIDEO_FPS,
)
from logging_utils import get_logger
from quality_filter import (
    is_used_before, mark_used, cached_path_for, remember_download, analyze_clip,
)
from video_effects import apply_motion, crossfade_concat

log = get_logger(__name__)


class PexelsError(RuntimeError):
    pass


# ══════════════════════════════════════════════════════════════════════════
# SEARCH
# ══════════════════════════════════════════════════════════════════════════

def search_pexels(query: str, count: int, session: requests.Session) -> list:
    if not PEXELS_API_KEY:
        raise PexelsError("PEXELS_API_KEY environment variable is not set.")

    page = random.randint(1, 8)
    try:
        r = session.get(
            PEXELS_SEARCH_URL,
            headers={"Authorization": PEXELS_API_KEY},
            params={
                "query": query, "orientation": "portrait", "size": "large",
                "per_page": 25, "page": page,
            },
            timeout=30,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("Pexels search failed for %r: %s", query, e)
        return []

    candidates = []
    for vid in r.json().get("videos", []):
        vid_id = vid.get("id")
        duration = vid.get("duration", 0)
        if duration < MIN_CLIP_DURATION or duration > MAX_CLIP_DURATION:
            continue
        if vid_id is None or is_used_before(vid_id):
            continue

        files = vid.get("video_files", [])
        portrait = [f for f in files if f.get("height", 0) > f.get("width", 1)]
        pool = portrait if portrait else files
        pool.sort(key=lambda x: x.get("width", 0) * x.get("height", 0), reverse=True)
        if not pool or not pool[0].get("link"):
            continue

        candidates.append({
            "id": vid_id,
            "url": pool[0]["link"],
            "duration": duration,
            "width": pool[0].get("width", 0),
            "height": pool[0].get("height", 0),
        })
        if len(candidates) >= count:
            break

    return candidates


def gather_candidates() -> list:
    queries = random.sample(NATURE_QUERIES, k=min(QUERIES_PER_RUN, len(NATURE_QUERIES)))
    all_candidates = []
    with requests.Session() as session:
        for q in queries:
            found = search_pexels(q, CLIPS_PER_QUERY, session)
            log.info("Pexels search '%s' -> %d candidates", q, len(found))
            all_candidates.extend(found)

    random.shuffle(all_candidates)
    return all_candidates


# ══════════════════════════════════════════════════════════════════════════
# DOWNLOAD + QUALITY GATE
# ══════════════════════════════════════════════════════════════════════════

def download_clip(url: str, dest: Path) -> None:
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=512 * 1024):
            if chunk:
                f.write(chunk)


def acquire_clip(candidate: dict, tmpdir: Path) -> Path:
    """Returns cached copy if we already downloaded this clip before, else fetches it."""
    cached = cached_path_for(candidate["id"])
    if cached.exists():
        log.info("  Using cached clip %s", candidate["id"])
        return cached

    raw = tmpdir / f"raw_{candidate['id']}.mp4"
    download_clip(candidate["url"], raw)
    raw.replace(cached)
    return cached


def collect_quality_clips(total_duration: float, tmpdir: Path) -> list:
    """
    Downloads and quality-gates candidates until we have enough footage,
    returns a list of (path, duration) for accepted clips only.

    Every downloaded candidate is fully analyzed (analyze_clip) regardless
    of whether it passes strict gating, so if strict gating rejects
    everything we can still rank all *usable* (non-corrupted) candidates by
    quality score and fall back to the best of them rather than failing the
    whole run. The pipeline only raises if every downloaded clip is
    genuinely corrupted/unreadable.
    """
    candidates = gather_candidates()
    if not candidates:
        raise PexelsError("Pexels returned no new candidates. Check API key/quota or query list.")

    needed = total_duration * DURATION_BUFFER
    accepted = []
    analyzed = []  # (path, duration, id, score) for every usable (non-corrupted) clip
    accumulated = 0.0

    for candidate in candidates:
        if accumulated >= needed:
            break
        try:
            path = acquire_clip(candidate, tmpdir)
        except requests.RequestException as e:
            log.warning("  Download failed for clip %s: %s", candidate["id"], e)
            continue

        result = analyze_clip(path, tmpdir)
        if result is None:
            # Genuinely corrupted/unreadable — the only case we discard outright.
            log.warning("  Discarding corrupted/unusable clip %s", candidate["id"])
            path.unlink(missing_ok=True)
            continue

        analyzed.append((path, candidate["duration"], candidate["id"], result["score"]))

        if result["passed"]:
            remember_download(candidate["id"], path, result["score"])
            mark_used(candidate["id"])
            accepted.append((path, candidate["duration"], result["score"]))
            accumulated += candidate["duration"]
            log.info("  Accepted clip %s (score=%.2f, %.1fs)", candidate["id"], result["score"], candidate["duration"])
        else:
            log.info("  Rejected clip %s: %s", candidate["id"], "; ".join(result["reasons"]))

    if not accepted:
        # FALLBACK STRATEGY: nothing passed strict gating. Rank every usable
        # (non-corrupted) candidate by quality score and take the best ones
        # instead of failing the run — a good-enough clip beats no clip.
        if not analyzed:
            raise PexelsError(
                "No usable clips: every downloaded candidate was corrupted or unreadable."
            )
        log.warning(
            "FALLBACK MODE: no clip passed strict quality gates this run — "
            "ranking all %d usable candidates by score and using the best instead "
            "of failing the pipeline.", len(analyzed),
        )
        analyzed.sort(key=lambda c: c[3], reverse=True)
        acc = 0.0
        for path, duration, cid, score in analyzed:
            if acc >= needed:
                break
            remember_download(cid, path, score)
            mark_used(cid)
            accepted.append((path, duration, score))
            acc += duration
            log.warning("  Fallback-selected clip %s (score=%.2f, %.1fs)", cid, score, duration)

    if not accepted:
        raise PexelsError(
            "No usable clips: every downloaded candidate was corrupted or unreadable."
        )

    # Highest quality first so the best footage anchors the edit; still shuffle
    # lightly so runs don't always open on the same theme.
    accepted.sort(key=lambda c: c[2], reverse=True)
    return [(p, d) for p, d, _ in accepted]


# ══════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

def build_background(total_duration: float, tmpdir: Path, out_path: Path) -> None:
    """
    Full pipeline: search -> quality gate -> motion edit each clip ->
    crossfade concat -> trim to exact audio duration.
    """
    clips = collect_quality_clips(total_duration, tmpdir)

    motion_paths, motion_durations = [], []
    for i, (path, duration) in enumerate(clips):
        motion_out = tmpdir / f"motion_{i:03d}.mp4"
        style = apply_motion(path, motion_out, duration, fps=VIDEO_FPS)
        log.info("  Applied motion '%s' to clip %d/%d", style, i + 1, len(clips))
        motion_paths.append(motion_out)
        motion_durations.append(duration)

    joined = tmpdir / "bg_joined.mp4"
    crossfade_concat(motion_paths, motion_durations, joined, fps=VIDEO_FPS)

    # Trim/pad to the exact audio duration
    cmd = [
        "ffmpeg", "-y", "-i", str(joined),
        "-t", str(total_duration),
        "-c:v", "libx264", "-preset", "medium", "-crf", "16",
        "-an", str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Final background trim failed: {result.stderr[-400:]}")
    log.info("Background ready -> %s (%.1fs, %d clips)", out_path.name, total_duration, len(clips))
