#!/usr/bin/env python3
"""
pexels_fetcher.py
Fetches simple, good-looking vertical nature footage from Pexels and
assembles it into a single background video for the Quran video.

Pipeline:
  1. Pick ONE visual theme (forest / mountain / ocean / winter /
     sky-and-night) for this reel and search Pexels using only that
     theme's cinematic queries — see THEME SELECTION note below. This
     keeps every clip in a reel visually related instead of jumping
     between unrelated environments.
  2. Download vertical videos only.
  3. Reject a downloaded clip if it's not vertical, below 720x1280,
     shorter than MIN_CLIP_DURATION, or corrupted/unreadable (all
     checked with a single cheap ffprobe call — see quality_filter.py).
  4. CONTENT FILTER (mandatory, fully automatic — see HUMAN FILTER note
     below): sample multiple frames across the whole clip and run them
     through a detector for people AND vehicles/transportation (cars,
     boats, bikes, buses, etc.), since Pexels "nature" results
     regularly include both. Any detection anywhere in the clip
     rejects it outright. No human review, no approval step — a
     rejected clip is simply discarded and the next candidate is tried,
     automatically, until enough clean clips are collected.
  5. Randomly select enough clean clips to cover the needed duration,
     pulling additional search rounds within the SAME theme
     automatically if the first batch isn't enough (see
     MAX_GATHER_ROUNDS).
  6. Trim each clip to a random 3-5s length, apply a subtle unified
     color grade, AND normalize it to a single common format
     (resolution, constant fps, pixel format, SAR, timebase) — see
     NORMALIZATION note below.
  7. Concatenate the now-identical, color-matched clips together.
  8. Add short, subtle crossfades between clips (~250-400ms).
  9. Use the result as the background for the Quran video.

THEME SELECTION NOTE:
Each reel should read as one intentional environment, not a stitched-
together mix of forest + ocean + desert clips. THEMED_QUERIES in
config.py groups cinematic search terms by mood. collect_clips() picks
one theme at random and stays with it across every search round for
that reel; it only reaches into a second theme as a last-resort
fallback if the first theme genuinely can't fill the needed duration
(logged clearly when this happens, since it's the exception, not the
norm).

HUMAN FILTER NOTE:
NO HUMAN MAY EVER APPEAR IN THE BACKGROUND — not even for one frame.
Pexels' keyword search is not a content guarantee: a "waterfall" or
"forest" search can and does return clips with a person walking
through, a hand entering frame, a distant figure, a silhouette, or a
reflection. Because of that, every downloaded clip is screened by
human_filter.clip_contains_person() (YOLOv8n over several sampled
frames spread across the whole clip, not just frame 1) before it's
allowed into the selected set. This is a hard gate: a positive
detection rejects the clip immediately and unconditionally, with no
manual step. See human_filter.py for the full detection approach and
its honestly-stated limits.

NORMALIZATION NOTE:
Pexels clips arrive with mixed frame rates (24/25/30/60fps), mixed
timebases, mixed sample aspect ratios, and sometimes non-yuv420p pixel
formats. ffmpeg's `xfade` filter requires every input to share the exact
same frame rate and timebase, or it fails outright (as opposed to
silently reconciling them). Rather than trying to make `xfade` handle
mismatched inputs, every clip is fully normalized to identical properties
*before* it ever reaches the crossfade filter:
  - scaled/cropped to VIDEO_WIDTH x VIDEO_HEIGHT
  - resampled to a constant VIDEO_FPS via the `fps` filter (handles
    24/25/30/60fps sources uniformly, duplicating/dropping frames as
    needed instead of leaving variable frame timing in place)
  - sample aspect ratio forced to 1:1 (`setsar=1`)
  - pixel format forced to yuv420p
  - encoded with `-fps_mode cfr` and an explicit, identical
    `-video_track_timescale`, so every trimmed clip has the same
    constant frame rate *and* the same container timebase
Because normalization happens once per clip up front, `crossfade_concat`
never needs to reconcile mismatched properties — by the time clips reach
it, they are already identical, so xfade only has to do the actual
cross-dissolve.
"""

import random
import subprocess
from pathlib import Path

import requests

from config import (
    PEXELS_API_KEY, PEXELS_SEARCH_URL, THEMED_QUERIES, CLIPS_PER_QUERY,
    QUERIES_PER_RUN, DURATION_BUFFER, MIN_CLIP_DURATION, MAX_CLIP_DURATION,
    CLIP_TRIM_MIN, CLIP_TRIM_MAX, VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS,
    TRANSITION_DURATION, GRADE_CONTRAST, GRADE_SATURATION, GRADE_BRIGHTNESS,
    GRADE_SHADOW_WARMTH, GRADE_HIGHLIGHT_WARMTH,
)
from logging_utils import get_logger
from quality_filter import is_used_before, mark_used, cached_path_for, validate_clip
from human_filter import clip_contains_person, HumanFilterError

log = get_logger(__name__)

# Fixed container timebase (in Hz) applied to every normalized clip so that
# xfade sees identical timebases on all inputs, regardless of what the
# source Pexels file used. 90000 is the standard MPEG-TS-style timescale
# and divides evenly for all target fps values we care about (24/25/30/60).
NORMALIZED_TIMESCALE = 90000

# Safety cap on how many additional Pexels search rounds collect_clips()
# will run to find enough human-free clips before giving up. Each round
# issues QUERIES_PER_RUN searches (see gather_candidates). Bounded so a
# query list that's unusually people-heavy can't turn into an unbounded
# loop burning API quota / CI minutes — it fails loudly instead.
MAX_GATHER_ROUNDS = 4


class PexelsError(RuntimeError):
    pass


# ══════════════════════════════════════════════════════════════════════════
# 1. SEARCH
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

        # 2. Download vertical videos only — pick the largest portrait file.
        files = vid.get("video_files", [])
        portrait = [f for f in files if f.get("height", 0) > f.get("width", 1)]
        if not portrait:
            continue
        portrait.sort(key=lambda x: x.get("width", 0) * x.get("height", 0), reverse=True)
        if not portrait[0].get("link"):
            continue

        candidates.append({
            "id": vid_id,
            "url": portrait[0]["link"],
            "duration": duration,
        })
        if len(candidates) >= count:
            break

    return candidates


def pick_theme() -> tuple:
    """Picks one random visual theme and returns (theme_name, query_list)."""
    theme_name = random.choice(list(THEMED_QUERIES.keys()))
    return theme_name, THEMED_QUERIES[theme_name]


def gather_candidates(query_pool: list) -> list:
    """Searches Pexels using queries drawn only from `query_pool` (a single
    theme's cinematic query list), so every candidate returned stays within
    one visual mood."""
    queries = random.sample(query_pool, k=min(QUERIES_PER_RUN, len(query_pool)))
    all_candidates = []
    with requests.Session() as session:
        for q in queries:
            found = search_pexels(q, CLIPS_PER_QUERY, session)
            log.info("Pexels search '%s' -> %d candidates", q, len(found))
            all_candidates.extend(found)

    random.shuffle(all_candidates)
    return all_candidates


# ══════════════════════════════════════════════════════════════════════════
# DOWNLOAD
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


# ══════════════════════════════════════════════════════════════════════════
# 3-4. VALIDATE + RANDOMLY SELECT
# ══════════════════════════════════════════════════════════════════════════

def _evaluate_candidate(candidate: dict, tmpdir: Path):
    """
    Downloads one candidate and runs it through every automatic gate:
    ffprobe validity, then (the hard requirement) the human-detection
    filter. Returns (path, trim_duration) if the clip is accepted, or
    None if it was rejected/failed at any stage. Never asks for manual
    input — every branch either accepts or rejects and moves on.
    """
    try:
        path = acquire_clip(candidate, tmpdir)
    except requests.RequestException as e:
        log.warning("  Download failed for clip %s: %s", candidate["id"], e)
        return None

    ok, reason = validate_clip(path)
    if not ok:
        log.info("  Rejected clip %s: %s", candidate["id"], reason)
        path.unlink(missing_ok=True)
        return None

    try:
        has_person = clip_contains_person(path, candidate["duration"], tmpdir)
    except HumanFilterError:
        # Detector itself is unusable (e.g. dependency missing) — this is
        # a hard requirement, so we cannot silently skip the check and
        # let unscreened footage through. Fail loudly instead.
        raise
    if has_person:
        log.info("  Rejected clip %s: contains a person or vehicle", candidate["id"])
        path.unlink(missing_ok=True)
        # Marked as used so it's never re-downloaded/re-scanned by a
        # future run — same dedup mechanism as a normally-consumed clip.
        mark_used(candidate["id"])
        return None

    trim_duration = round(random.uniform(CLIP_TRIM_MIN, CLIP_TRIM_MAX), 2)
    mark_used(candidate["id"])
    log.info("  Selected clip %s (trim=%.1fs)", candidate["id"], trim_duration)
    return path, trim_duration


def collect_clips(total_duration: float, tmpdir: Path) -> list:
    """
    Downloads candidates, validates each (ffprobe check + mandatory
    human-detection filter — see human_filter.py), and randomly selects
    enough human-free clips to cover total_duration (with a small buffer
    for trimming/crossfades). Returns a list of (path, trim_duration)
    pairs.

    Fully automatic end to end: if one search round doesn't turn up
    enough usable clips (e.g. an unusually people/vehicle-heavy batch),
    it automatically pulls another round of candidates from the SAME
    theme and keeps going — up to MAX_GATHER_ROUNDS — with no manual
    intervention. Stays on one visual theme throughout so the reel
    reads as one cohesive environment; only falls back to a second
    theme if the first one genuinely can't fill the needed duration.
    """
    needed = total_duration * DURATION_BUFFER
    selected = []
    accumulated = 0.0
    tried_ids = set()

    theme_name, theme_queries = pick_theme()
    log.info("Visual theme for this reel: %s", theme_name)
    remaining_themes = [t for t in THEMED_QUERIES if t != theme_name]

    round_num = 0
    while round_num < MAX_GATHER_ROUNDS:
        round_num += 1
        if accumulated >= needed:
            break

        candidates = [c for c in gather_candidates(theme_queries) if c["id"] not in tried_ids]
        if not candidates:
            log.info("Round %d (%s): no new candidates found", round_num, theme_name)
        else:
            log.info("Round %d (%s): evaluating %d candidates (%.1fs / %.1fs collected so far)",
                      round_num, theme_name, len(candidates), accumulated, needed)
            for candidate in candidates:
                if accumulated >= needed:
                    break
                tried_ids.add(candidate["id"])
                result = _evaluate_candidate(candidate, tmpdir)
                if result is None:
                    continue
                path, trim_duration = result
                selected.append((path, trim_duration))
                accumulated += trim_duration

        # Last-resort fallback: the chosen theme couldn't fill the reel on
        # its own even after every round. Switch to one more theme rather
        # than failing the whole run — logged clearly since visual
        # consistency is being relaxed as an exception, not the default.
        if accumulated < needed and round_num >= MAX_GATHER_ROUNDS and remaining_themes:
            theme_name = remaining_themes.pop(random.randrange(len(remaining_themes)))
            theme_queries = THEMED_QUERIES[theme_name]
            log.warning("Primary theme couldn't fill the reel; falling back to theme: %s", theme_name)
            round_num = 0

    if not selected:
        raise PexelsError(
            "No usable clips: every downloaded candidate was invalid, corrupted, "
            "unreadable, or contained a person/vehicle."
        )

    random.shuffle(selected)
    return selected


# ══════════════════════════════════════════════════════════════════════════
# 5. TRIM / NORMALIZE
# ══════════════════════════════════════════════════════════════════════════

def trim_and_normalize(src: Path, dst: Path, duration: float) -> None:
    """
    Trims `src` to `duration` seconds and normalizes it to a single common
    format so that every clip fed into crossfade_concat is guaranteed
    identical on every property xfade cares about:

      - VIDEO_WIDTH x VIDEO_HEIGHT (scale + center crop)
      - constant VIDEO_FPS (the `fps` filter resamples 24/25/30/60fps
        sources onto one constant frame rate, unlike `-r` alone which can
        leave irregular frame timing in place)
      - sample aspect ratio forced to 1:1 (setsar=1) so differing SAR
        metadata from source clips can't produce a mismatched display size
      - yuv420p pixel format (normalizes away 4:2:2/4:4:4/other formats)
      - a fixed, identical container timebase (-video_track_timescale)
        and constant frame rate muxing (-fps_mode cfr), so every trimmed
        clip's timebase matches exactly, not just its frame rate

    This is a real re-encode (not `-c copy`) specifically because only a
    re-encode can rewrite fps/SAR/pixel format/timebase; xfade is never
    relied upon to reconcile any of these. The same re-encode is also
    used to bake in a subtle, identical color grade (see GRADE_* in
    config.py) on every clip, so footage pulled from different Pexels
    sources within a theme doesn't visibly jump between warm/cold or
    flat/saturated looks once concatenated.
    """
    vf = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
        f"fps={VIDEO_FPS},"
        f"setsar=1,"
        f"eq=contrast={GRADE_CONTRAST}:saturation={GRADE_SATURATION}:brightness={GRADE_BRIGHTNESS},"
        f"colorbalance=rs={GRADE_SHADOW_WARMTH}:bs={-GRADE_SHADOW_WARMTH}:"
        f"rh={GRADE_HIGHLIGHT_WARMTH}:bh={-GRADE_HIGHLIGHT_WARMTH},"
        f"format=yuv420p"
    )
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-t", str(duration),
        "-vf", vf,
        "-r", str(VIDEO_FPS),
        "-fps_mode", "cfr",
        "-video_track_timescale", str(NORMALIZED_TIMESCALE),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-an",
        str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Trim/normalize failed on {src.name}: {result.stderr[-400:]}")


# ══════════════════════════════════════════════════════════════════════════
# 6-7. CONCATENATE WITH CROSSFADES
# ══════════════════════════════════════════════════════════════════════════

def crossfade_concat(clip_paths: list, durations: list, out_path: Path,
                      transition: float = TRANSITION_DURATION, fps: int = VIDEO_FPS) -> None:
    """
    Joins already-normalized clips with xfade crossfade transitions.

    By this point every clip in clip_paths has already been produced by
    trim_and_normalize(), so all inputs share identical resolution, fps,
    SAR, pixel format, and timebase. xfade is only asked to do the actual
    cross-dissolve here — no scaling, fps conversion, or format handling
    happens in this filter graph, since doing that work twice (once in
    normalization, once implicitly via xfade) is exactly what caused the
    "frame rate does not match" / "timebase mismatch" failures.
    """
    if len(clip_paths) == 1:
        cmd = ["ffmpeg", "-y", "-i", str(clip_paths[0]), "-c", "copy", str(out_path)]
        subprocess.run(cmd, check=True, capture_output=True)
        return

    inputs = []
    for p in clip_paths:
        inputs += ["-i", str(p)]

    filter_parts = []
    cumulative = 0.0
    last_label = "0:v"

    for i in range(1, len(clip_paths)):
        offset = max(cumulative + durations[i - 1] - transition, 0.1)
        out_label = f"v{i}" if i < len(clip_paths) - 1 else "vout"
        filter_parts.append(
            f"[{last_label}][{i}:v]xfade=transition=fade:duration={transition}:offset={offset:.3f}[{out_label}]"
        )
        cumulative += durations[i - 1] - transition
        last_label = out_label

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-r", str(fps),
        "-fps_mode", "cfr",
        "-video_track_timescale", str(NORMALIZED_TIMESCALE),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-an",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Crossfade concat failed: {result.stderr[-500:]}")
    log.info("Crossfade background assembled -> %s", out_path.name)


# ══════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

def build_background(total_duration: float, tmpdir: Path, out_path: Path) -> None:
    """
    Full pipeline: search -> validate -> select -> trim+normalize ->
    crossfade concat -> trim to exact audio duration.
    """
    clips = collect_clips(total_duration, tmpdir)

    trimmed_paths, trimmed_durations = [], []
    for i, (path, duration) in enumerate(clips):
        trimmed_out = tmpdir / f"trim_{i:03d}.mp4"
        trim_and_normalize(path, trimmed_out, duration)
        trimmed_paths.append(trimmed_out)
        trimmed_durations.append(duration)

    joined = tmpdir / "bg_joined.mp4"
    crossfade_concat(trimmed_paths, trimmed_durations, joined, fps=VIDEO_FPS)

    # Trim/pad to the exact audio duration
    cmd = [
        "ffmpeg", "-y", "-i", str(joined),
        "-t", str(total_duration),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-an", str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Final background trim failed: {result.stderr[-400:]}")
    log.info("Background ready -> %s (%.1fs, %d clips)", out_path.name, total_duration, len(clips))
