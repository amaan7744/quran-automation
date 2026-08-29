#!/usr/bin/env python3
"""
pexels_fetcher.py
Fetches good-looking vertical cinematic footage from Pexels and
assembles it into a single background video for the Quran video.

Pipeline:
  1. Search Pexels using only the CURRENT category's cinematic queries
     from a category fallback chain chosen upstream by visual_engine.py
     (item 3/11 of the brief) — see CATEGORY SELECTION note below. This
     keeps every clip in a reel visually related instead of jumping
     between unrelated environments, while still allowing a graceful,
     visually-coherent fallback if the primary category runs dry.
  2. Download vertical videos only.
  3. Reject a downloaded clip if it's not vertical, below the
     configured minimum resolution (MIN_CLIP_WIDTH x MIN_CLIP_HEIGHT —
     1080x1920 by default, see SOURCE QUALITY TIERS below), shorter
     than MIN_CLIP_DURATION, or corrupted/unreadable (all checked with
     a single cheap ffprobe call — see quality_filter.py).
  4. CONTENT FILTER (mandatory, fully automatic — see HUMAN FILTER note
     below): sample multiple frames across the whole clip and run them
     through a detector for people AND vehicles/transportation (cars,
     boats, bikes, buses, etc.), since Pexels "nature" results
     regularly include both. Any detection anywhere in the clip
     rejects it outright. No human review, no approval step — a
     rejected clip is simply discarded and the next candidate is tried,
     automatically, until enough clean clips are collected.
  5. Randomly select enough clean clips to cover the needed duration,
     pulling additional search rounds within the SAME category
     automatically if the first batch isn't enough (see
     MAX_GATHER_ROUNDS), then moving down the fallback chain (item 11)
     if the category genuinely can't fill the reel.
  6. Trim each clip to a random length within the template's
     clip_duration_range, apply a per-clip motion style (item 8, via
     video_effects.apply_motion), apply the template's color grade
     (item 9), AND normalize it to a single common format (resolution,
     constant fps, pixel format, SAR, timebase) — see NORMALIZATION
     note below.
  7. Concatenate the now-identical, color-matched, motion-applied clips
     together.
  8. Add short, subtle crossfades between clips using the template's
     chosen transition style (~250-400ms).
  9. Use the result as the background for the Quran video.

CATEGORY SELECTION NOTE:
Each reel should read as one intentional environment, not a stitched-
together mix of forest + ocean + desert clips. visual_themes.py groups
cinematic search terms by category and defines a fallback chain per
category (item 11: primary -> related -> general cinematic). The
caller (visual_engine.py) picks the category chain for this reel based
on its mood; collect_clips() stays on the first category in the chain
across every search round, and only advances to the next category in
the chain as a last resort if that category genuinely can't fill the
needed duration (logged clearly when this happens, since it's the
exception, not the norm). Legacy callers that don't have an
upstream-selected chain still work: build_background() defaults to a
single random category from the full VISUAL_CATEGORIES pool.

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

SOURCE QUALITY TIERS (item 3/10 of the 2K pass):
Pexels returns several resolution variants per clip in `video_files`.
search_pexels() now explicitly tiers these — preferring 2160p (4K) >
1440p > 1080p — rather than just "the largest available," and records
which tier was actually used (source_width/source_height, plus a
human-readable "source_quality_tier") on every candidate, so the final
video's metadata can honestly report whether a given clip was native
2K-or-better footage or a 1080p fallback (never silently presented as
native 2K). MIN_CLIP_WIDTH/MIN_CLIP_HEIGHT in config.py set a hard
floor at 1080p — anything softer than that is rejected outright rather
than accepted and stretched two resolution tiers up to fill the frame.

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
from collections import Counter
from pathlib import Path

import requests

from config import (
    PEXELS_API_KEY, PEXELS_SEARCH_URL, CLIPS_PER_QUERY,
    QUERIES_PER_RUN, DURATION_BUFFER, MIN_CLIP_DURATION, MAX_CLIP_DURATION,
    CLIP_TRIM_MIN, CLIP_TRIM_MAX, VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS,
    TRANSITION_DURATION, INTERMEDIATE_ENCODE_CRF, INTERMEDIATE_ENCODE_PRESET,
)
from visual_themes import VISUAL_CATEGORIES, COLOR_GRADES
from logging_utils import get_logger
from quality_filter import is_used_before, mark_used, cached_path_for, validate_clip
from human_filter import clip_contains_person, HumanFilterError
from video_effects import motion_filter_fragment, atmosphere_overlay_fragment, pick_motion_style

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

# Named source-resolution tiers (item 3/10 of the 2K pass), checked in
# this order — highest quality first. A file only needs to meet the
# WIDTH threshold since Pexels portrait files are already true 9:16 (a
# "2160p" portrait file is 2160x3840, so checking width>=2160 is
# sufficient and avoids being overly strict about a source that's
# e.g. 2160x3830 due to minor encoder rounding).
SOURCE_QUALITY_TIERS = [
    ("2160p", 2160),
    ("1440p", 1440),
    ("1080p", 1080),
]


class PexelsError(RuntimeError):
    pass


def _classify_source_tier(width: int) -> str:
    for tier_name, min_width in SOURCE_QUALITY_TIERS:
        if width >= min_width:
            return tier_name
    return "below_1080p"


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

        # 2. Download vertical videos only. Prefer the highest available
        # resolution tier (2160p > 1440p > 1080p — item 3/10 of the 2K
        # pass) rather than blindly "the largest available," so a
        # 4K-capable source is never accidentally passed over for a
        # merely-large-for-1080p one. Sorting portrait files by area
        # descending first, THEN classifying the winner's tier, means
        # we still get the single best file Pexels actually offers for
        # this clip (Pexels typically exposes one file per resolution
        # tier, not several competing options within the same tier).
        files = vid.get("video_files", [])
        portrait = [f for f in files if f.get("height", 0) > f.get("width", 1)]
        if not portrait:
            continue
        portrait.sort(key=lambda x: x.get("width", 0) * x.get("height", 0), reverse=True)
        best = portrait[0]
        if not best.get("link"):
            continue

        source_width, source_height = best.get("width", 0), best.get("height", 0)
        candidates.append({
            "id": vid_id,
            "url": best["link"],
            "source_width": source_width,
            "source_height": source_height,
            "source_quality_tier": _classify_source_tier(source_width),
            "duration": duration,
        })
        if len(candidates) >= count:
            break

    return candidates


def pick_category() -> tuple:
    """Picks one random visual category and returns (category_name, query_list).
    Only used when no upstream category chain was supplied (legacy/manual
    call path) — the normal pipeline path always gets its chain from
    visual_engine.choose_plan()."""
    category_name = random.choice(list(VISUAL_CATEGORIES.keys()))
    return category_name, VISUAL_CATEGORIES[category_name]


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

def _evaluate_candidate(candidate: dict, tmpdir: Path, clip_duration_range: tuple):
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

    lo, hi = clip_duration_range
    trim_duration = round(random.uniform(lo, hi), 2)
    mark_used(candidate["id"])
    log.info("  Selected clip %s (trim=%.1fs, source=%dx%d [%s])", candidate["id"], trim_duration,
              candidate.get("source_width", 0), candidate.get("source_height", 0),
              candidate.get("source_quality_tier", "unknown"))
    return path, trim_duration, {
        "source_width": candidate.get("source_width", 0),
        "source_height": candidate.get("source_height", 0),
        "source_quality_tier": candidate.get("source_quality_tier", "unknown"),
    }


def collect_clips(total_duration: float, tmpdir: Path, category_chain: list = None,
                   clip_duration_range: tuple = (CLIP_TRIM_MIN, CLIP_TRIM_MAX)) -> tuple:
    """
    Downloads candidates, validates each (ffprobe check + mandatory
    human-detection filter — see human_filter.py), and randomly selects
    enough human-free clips to cover total_duration (with a small buffer
    for trimming/crossfades). Returns (clips, category_used) where clips
    is a list of (path, trim_duration, source_info) triples — source_info
    is {"source_width", "source_height", "source_quality_tier"} for that
    clip's chosen Pexels file (item 3/10 of the 2K pass).

    Fully automatic end to end: if one search round doesn't turn up
    enough usable clips (e.g. an unusually people/vehicle-heavy batch),
    it automatically pulls another round of candidates from the SAME
    category and keeps going — up to MAX_GATHER_ROUNDS — with no manual
    intervention. Stays on one visual category throughout so the reel
    reads as one cohesive environment; only advances to the next
    category in `category_chain` (see visual_themes.fallback_chain) if
    the current one genuinely can't fill the needed duration.

    `category_chain` defaults to a single random category (legacy/manual
    call path) when not supplied by visual_engine.py.
    """
    if not category_chain:
        name, _ = pick_category()
        category_chain = [name]
    chain = list(category_chain)

    needed = total_duration * DURATION_BUFFER
    selected = []
    selected_categories = []  # parallel list: which category each selected clip came from
    accumulated = 0.0
    tried_ids = set()

    category_name = chain.pop(0)
    category_queries = VISUAL_CATEGORIES[category_name]
    log.info("Visual category for this reel: %s", category_name)

    round_num = 0
    while round_num < MAX_GATHER_ROUNDS:
        round_num += 1
        if accumulated >= needed:
            break

        candidates = [c for c in gather_candidates(category_queries) if c["id"] not in tried_ids]
        if not candidates:
            log.info("Round %d (%s): no new candidates found", round_num, category_name)
        else:
            log.info("Round %d (%s): evaluating %d candidates (%.1fs / %.1fs collected so far)",
                      round_num, category_name, len(candidates), accumulated, needed)
            for candidate in candidates:
                if accumulated >= needed:
                    break
                tried_ids.add(candidate["id"])
                result = _evaluate_candidate(candidate, tmpdir, clip_duration_range)
                if result is None:
                    continue
                path, trim_duration, source_info = result
                selected.append((path, trim_duration, source_info))
                selected_categories.append(category_name)
                accumulated += trim_duration

        # Fallback: the chosen category couldn't fill the reel on its
        # own even after every round. Advance to the next category in
        # the chain (item 11: primary -> related -> general cinematic)
        # rather than failing the whole run — logged clearly since
        # visual consistency is being relaxed as an exception, not the
        # default.
        if accumulated < needed and round_num >= MAX_GATHER_ROUNDS and chain:
            category_name = chain.pop(0)
            category_queries = VISUAL_CATEGORIES[category_name]
            log.warning("Primary category couldn't fill the reel; falling back to category: %s", category_name)
            round_num = 0

    if not selected:
        raise PexelsError(
            "No usable clips: every downloaded candidate was invalid, corrupted, "
            "unreadable, or contained a person/vehicle."
        )

    # The category actually reported for this reel is whichever category
    # contributed the most selected clips — NOT necessarily the last
    # category the loop happened to be trying when it exited (which may
    # have contributed zero clips, e.g. if every fallback category ran
    # dry). This keeps visual_category metadata (item 13) honest about
    # what's actually in the reel.
    category_used = Counter(selected_categories).most_common(1)[0][0]

    # Shuffle clip order, but keep each clip paired with the category it
    # came from so a mixed-fallback reel doesn't lose that information —
    # shuffle indices instead of the flat (path, duration) list.
    order = list(range(len(selected)))
    random.shuffle(order)
    selected = [selected[i] for i in order]
    return selected, category_used




# ══════════════════════════════════════════════════════════════════════════
# 5. TRIM / NORMALIZE
# ══════════════════════════════════════════════════════════════════════════

def trim_and_normalize(src: Path, dst: Path, duration: float, grade: dict,
                        motion_style: str = "static", atmosphere_intensity: str = "low") -> None:
    """
    Trims `src` to `duration` seconds, applies a per-clip motion style
    (item 8) and a color grade (item 9), and normalizes the result to a
    single common format so that every clip fed into crossfade_concat is
    guaranteed identical on every property xfade cares about:

      - VIDEO_WIDTH x VIDEO_HEIGHT (via the motion fragment's own
        scale+crop, or scale+crop directly for "static")
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
    relied upon to reconcile any of these. Motion, color grade, and an
    optional very light atmospheric grain overlay (item 6) are baked
    into this SAME re-encode pass (rather than separate ffmpeg calls)
    so a clip is only ever re-encoded once. `grade` is one of
    visual_themes.COLOR_GRADES's value dicts.
    """
    motion_fragment = motion_filter_fragment(motion_style, duration, VIDEO_FPS, VIDEO_WIDTH, VIDEO_HEIGHT)
    atmosphere_fragment = atmosphere_overlay_fragment(atmosphere_intensity)

    vf_parts = [
        motion_fragment,
        f"fps={VIDEO_FPS}",
        "setsar=1",
        f"eq=contrast={grade['contrast']}:saturation={grade['saturation']}:brightness={grade['brightness']}",
        f"colorbalance=rs={grade['shadow_warmth']}:bs={-grade['shadow_warmth']}:"
        f"rh={grade['highlight_warmth']}:bh={-grade['highlight_warmth']}",
    ]
    if atmosphere_fragment:
        vf_parts.append(atmosphere_fragment)
    vf_parts.append("format=yuv420p")
    vf = ",".join(vf_parts)

    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-t", str(duration),
        "-vf", vf,
        "-r", str(VIDEO_FPS),
        "-fps_mode", "cfr",
        "-video_track_timescale", str(NORMALIZED_TIMESCALE),
        "-c:v", "libx264", "-preset", INTERMEDIATE_ENCODE_PRESET, "-crf", str(INTERMEDIATE_ENCODE_CRF),
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
                      transition_style: str = "fade",
                      transition: float = TRANSITION_DURATION, fps: int = VIDEO_FPS) -> None:
    """
    Joins already-normalized clips with xfade crossfade transitions,
    using ONE consistent transition_style for the whole reel (chosen
    once per reel by visual_engine.py — item 7/9: a template's
    transition_style — so the edit reads as one intentional choice
    rather than a random transition per cut).

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
            f"[{last_label}][{i}:v]xfade=transition={transition_style}:duration={transition}:offset={offset:.3f}[{out_label}]"
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
        "-c:v", "libx264", "-preset", INTERMEDIATE_ENCODE_PRESET, "-crf", str(INTERMEDIATE_ENCODE_CRF),
        "-an",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # xfade's chosen transition_style can, in rare cases, be a name
        # this ffmpeg build doesn't support. Retry once with the always-
        # available "fade" rather than failing the whole render over a
        # cosmetic transition choice.
        if transition_style != "fade":
            log.warning("Crossfade with transition '%s' failed, retrying with 'fade': %s",
                        transition_style, result.stderr[-300:])
            crossfade_concat(clip_paths, durations, out_path, "fade", transition, fps)
            return
        raise RuntimeError(f"Crossfade concat failed: {result.stderr[-500:]}")
    log.info("Crossfade background assembled -> %s (transition=%s)", out_path.name, transition_style)


# ══════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

def build_background(total_duration: float, tmpdir: Path, out_path: Path,
                      category_chain: list = None, color_grade: str = "neutral_cinematic",
                      motion_pool: list = None, transition_style: str = "fade",
                      clip_duration_range: tuple = (CLIP_TRIM_MIN, CLIP_TRIM_MAX),
                      fps: int = VIDEO_FPS) -> dict:
    """
    Full pipeline: search -> validate -> select -> trim+normalize (with
    per-clip motion + color grade + light atmosphere) -> crossfade
    concat (with the chosen transition style). crossfade_concat's own
    output IS `out_path` directly — there is deliberately no extra
    "trim to exact duration" re-encode pass after it (item 5 of the 2K
    pass: "avoid unnecessary repeated lossy encoding"): the background
    is already over-provisioned to exceed total_duration (see
    DURATION_BUFFER in collect_clips), and merge_main_segment's own
    `-t total_duration` on the FINAL composite already truncates it to
    the exact recitation length — re-trimming it here first was a
    whole extra full-resolution encode generation that never changed
    the final output.

    Every parameter beyond total_duration/tmpdir/out_path is optional
    with a safe default, so this remains callable exactly as before for
    any legacy/manual use. The normal pipeline path always supplies them
    via visual_engine.build_background_for_plan().

    Returns {"motion_styles": [...], "source_clips": [...]} — the list
    of motion styles actually applied (one per clip, in concatenation
    order) and each clip's source resolution/quality tier (item 3/10 of
    the 2K pass) — both consumed by build_video.py for the video's
    metadata.
    """
    clips, category_used = collect_clips(total_duration, tmpdir, category_chain, clip_duration_range)
    grade = COLOR_GRADES.get(color_grade, COLOR_GRADES["neutral_cinematic"])

    # Atmosphere intensity travels with the template, but build_background
    # only receives category_chain/color_grade/motion_pool/transition
    # from visual_engine — infer a sensible default here rather than
    # requiring yet another parameter, since this is a light polish
    # layer, not a correctness-critical one.
    atmosphere_intensity = "medium"

    trimmed_paths, trimmed_durations, motion_styles_used, source_clips = [], [], [], []
    for i, (path, duration, source_info) in enumerate(clips):
        motion_style = pick_motion_style(motion_pool)
        trimmed_out = tmpdir / f"trim_{i:03d}.mp4"
        trim_and_normalize(path, trimmed_out, duration, grade, motion_style, atmosphere_intensity)
        trimmed_paths.append(trimmed_out)
        trimmed_durations.append(duration)
        motion_styles_used.append(motion_style)
        source_clips.append(source_info)

    crossfade_concat(trimmed_paths, trimmed_durations, out_path, transition_style=transition_style, fps=fps)

    below_2k = sum(1 for c in source_clips if c["source_quality_tier"] not in ("2160p", "1440p"))
    if below_2k:
        log.info("%d/%d clips in this reel used sub-2K source footage (1080p fallback) — see metadata.",
                  below_2k, len(source_clips))
    log.info("Background ready -> %s (%.1fs, %d clips, category=%s)",
              out_path.name, total_duration, len(clips), category_used)
    return {"motion_styles": motion_styles_used, "source_clips": source_clips, "category_used": category_used}
