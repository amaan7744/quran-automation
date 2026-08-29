#!/usr/bin/env python3
"""
performance_metadata.py
Writes rich per-video metadata (item 13 of the original brief) and
implements an honest experimentation framework (item 14) that is
statistically cautious about what counts as "proven" (item 5 of the
final quality pass), plus a transparent, inspectable performance score
(item 6).

This module does NOT invent or fabricate performance numbers. It only:
  - stores whatever metadata build_video.py hands it for each render,
    keyed by a canonical sha256 hash of the actual rendered video file
    (see compute_video_hash) — the SAME hash upload.py already computes
    for its own dedup check, so a video's generation record, its
    platform IDs, and its later performance numbers all line up under
    one key without any fragile title-matching,
  - stores whatever platform video IDs upload.py attaches after a
    successful upload (see attach_platform_ids), and
  - stores whatever real performance numbers an external analytics
    ingestion step feeds in later (see record_performance and, for the
    actual ingestion logic, analytics_ingest.py) — nothing here talks
    to YouTube/Meta directly.

Until real performance data exists — and, per item 5, until there is
ENOUGH of it — every template/mood/duration choice is a plain random
experiment. That is the correct, honest behavior for a system with
no/insufficient data yet, not a bug to work around.
"""

import hashlib
import json
import random
import time
from pathlib import Path

from config import (
    ANALYTICS_FILE, EXPERIMENT_EXPLOIT_RATIO, MIN_SAMPLES_TO_TRUST,
    WEAK_SIGNAL_THRESHOLD, STRONG_SIGNAL_THRESHOLD, WEAK_SIGNAL_EXPLOIT_SCALE,
    DURATION_BUCKETS, PERFORMANCE_SCORE_WEIGHTS,
)
from visual_themes import TEMPLATE_NAMES
from logging_utils import get_logger

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════
# CANONICAL VIDEO IDENTITY
# ══════════════════════════════════════════════════════════════════════════

def compute_video_hash(path: Path) -> str:
    """
    sha256 of the actual rendered video file — the single canonical key
    used to tie together a video's generation metadata (written by
    build_video.py), its platform IDs (attached by upload.py after
    upload), and its eventual real performance numbers (written by
    analytics_ingest.py). upload.py's own dedup check hashes the exact
    same file the exact same way, so the two are always consistent
    without either side needing to trust a value written by the other.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ══════════════════════════════════════════════════════════════════════════
# STORE
# ══════════════════════════════════════════════════════════════════════════

def _load_store() -> dict:
    if ANALYTICS_FILE.exists():
        try:
            return json.loads(ANALYTICS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("analytics.json unreadable (%s) — starting a fresh store.", e)
    return {"videos": {}, "template_stats": {}, "duration_stats": {}}


def _save_store(store: dict) -> None:
    try:
        ANALYTICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        ANALYTICS_FILE.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("Failed to write analytics.json: %s", e)


# ══════════════════════════════════════════════════════════════════════════
# STATISTICAL CONFIDENCE (item 5: "do not overfit early data")
# ══════════════════════════════════════════════════════════════════════════
# Named, documented tiers rather than a single hidden cutoff, so it's
# always inspectable why a template did or didn't get "trusted":
#
#   < WEAK_SIGNAL_THRESHOLD (5)         -> "none":   exploration only
#   WEAK_SIGNAL_THRESHOLD..MIN_SAMPLES  -> "weak":    signal exists but
#                                                      only partially
#                                                      trusted (see
#                                                      WEAK_SIGNAL_EXPLOIT_SCALE)
#   MIN_SAMPLES..STRONG_SIGNAL          -> "usable":  fully trusted at
#                                                      the normal
#                                                      explore/exploit
#                                                      ratio
#   >= STRONG_SIGNAL_THRESHOLD (20)     -> "strong":  fully trusted
#
# One video with 500 views is still just `count=1` here — sample count
# is measured in NUMBER OF VIDEOS carrying a given template/bucket that
# have received a real score, not raw view count, so a single viral
# outlier can never look like "proven."

def confidence_tier(count: int) -> str:
    if count < WEAK_SIGNAL_THRESHOLD:
        return "none"
    if count < MIN_SAMPLES_TO_TRUST:
        return "weak"
    if count < STRONG_SIGNAL_THRESHOLD:
        return "usable"
    return "strong"


def _effective_exploit_probability(count: int, base_ratio: float) -> float:
    """
    Scales the base 70-80%-style exploit ratio down for a "weak" signal
    rather than treating MIN_SAMPLES_TO_TRUST as a hard on/off switch —
    a template with 6 samples gets SOME extra trust over pure random,
    but nowhere near as much as one with 15. "none" never exploits at
    all; "usable" and "strong" both get the full base ratio (strong
    doesn't get boosted further — more data earns confidence, not an
    ever-increasing lock-in that would stop further exploration).
    """
    tier = confidence_tier(count)
    if tier == "none":
        return 0.0
    if tier == "weak":
        return base_ratio * WEAK_SIGNAL_EXPLOIT_SCALE
    return base_ratio


# ══════════════════════════════════════════════════════════════════════════
# EXPLORE / EXPLOIT SELECTION
# ══════════════════════════════════════════════════════════════════════════

def _best_key(stats: dict, candidates: list, metric: str = "avg_score"):
    """
    Returns (best_key, count, tier) for the candidate with the highest
    average `metric` among entries that have reached at least the "weak"
    tier — or (None, 0, "none") if nothing qualifies, so the caller
    falls back to a plain weighted-random choice.
    """
    best_key, best_val, best_count = None, float("-inf"), 0
    for key in candidates:
        entry = stats.get(key)
        if not entry:
            continue
        count = entry.get("count", 0)
        if confidence_tier(count) == "none":
            continue
        val = entry.get(metric, 0.0)
        if val > best_val:
            best_key, best_val, best_count = key, val, count
    tier = confidence_tier(best_count) if best_key else "none"
    return best_key, best_count, tier


def pick_template(mood: str, rng: random.Random = None) -> str:
    """
    Explore/exploit template pick for this mood (item 14), gated by
    statistical confidence (item 5) — see confidence_tier() and
    _effective_exploit_probability(). A "weak" signal only partially
    biases the pick; a candidate below the "weak" threshold is never
    exploited at all, and this remains a plain random pick across all
    templates until enough real per-template scores exist for THIS mood.
    """
    rng = rng or random
    store = _load_store()
    mood_stats = store.get("template_stats", {}).get(mood, {})

    best, count, tier = _best_key(mood_stats, TEMPLATE_NAMES)
    if best and rng.random() < _effective_exploit_probability(count, EXPERIMENT_EXPLOIT_RATIO):
        log.info("Template pick for mood '%s': exploiting %s (confidence=%s, n=%d)", mood, best, tier, count)
        return best

    choice = rng.choice(TEMPLATE_NAMES)
    log.info("Template pick for mood '%s': experimenting with %s (best candidate confidence=%s)", mood, choice, tier)
    return choice


def pick_duration_bucket(rng: random.Random = None) -> tuple:
    """
    Same graduated-confidence explore/exploit as pick_template(), for
    the target/hard-max duration window (item 12/14). Ayah integrity
    always wins over hitting a bucket exactly — the caller
    (build_video.fit_batch_to_duration) already guarantees an ayah is
    never split regardless of which bucket is chosen here.
    """
    rng = rng or random
    store = _load_store()
    duration_stats = store.get("duration_stats", {})
    bucket_names = list(DURATION_BUCKETS.keys())

    best, count, tier = _best_key(duration_stats, bucket_names)
    if best and rng.random() < _effective_exploit_probability(count, EXPERIMENT_EXPLOIT_RATIO):
        log.info("Duration bucket: exploiting %s (confidence=%s, n=%d)", best, tier, count)
        return DURATION_BUCKETS[best]

    choice = rng.choice(bucket_names)
    log.info("Duration bucket: experimenting with %s (best candidate confidence=%s)", choice, tier)
    return DURATION_BUCKETS[choice]


# ══════════════════════════════════════════════════════════════════════════
# PERFORMANCE SCORE (item 6: transparent, inspectable, age-normalized)
# ══════════════════════════════════════════════════════════════════════════
# The formula is deliberately simple and fully visible here — nothing
# about it is hidden inside a black box. Each component is optional:
# if a metric wasn't retrievable from the platform API (see
# analytics_ingest.py), its term is dropped entirely and the remaining
# weights are renormalized to still sum to 1.0, rather than treating a
# missing metric as a 0 (which would unfairly punish a video just
# because one particular number wasn't available).
#
#   retention_component      = average_percentage_viewed   (0..1)
#   pace_component            = (views / age_days) / channel_baseline_views_per_day
#                                — "views relative to channel baseline,
#                                normalized by age" (item 6), NOT raw
#                                view count, so an older video's bigger
#                                total view count doesn't automatically
#                                outscore a newer one. Clipped to
#                                [0, 3] so one outlier-viral video can't
#                                blow the whole score off the scale.
#   subscriber_component      = subscribers_gained / views, scaled x1000
#   engagement_component      = (likes + comments) / views, scaled x100
#
# PERFORMANCE_SCORE_WEIGHTS (config.py) default to
# {"retention": 0.50, "pace": 0.25, "subscriber": 0.15, "engagement": 0.10}.

def compute_performance_score(metrics: dict, channel_baseline_views_per_day: float | None) -> dict:
    """
    Returns {"score": float|None, "components": {name: value_or_None},
    "missing_metrics": [names]} — never a bare number — so a template's
    score can always be explained, per item 6 ("do NOT hide the
    calculation... possible to inspect why a video scored highly").
    `metrics` is expected to carry whatever subset of views / likes /
    comments / subscribers_gained / average_percentage_viewed /
    age_days the ingestion step actually managed to retrieve; anything
    absent or explicitly None is treated as unavailable, not zero.
    """
    components = {}
    missing = []

    retention = metrics.get("average_percentage_viewed")
    components["retention"] = retention if retention is not None else None
    if retention is None:
        missing.append("average_percentage_viewed")

    views = metrics.get("views")
    age_days = metrics.get("age_days")
    if views is not None and age_days and age_days > 0 and channel_baseline_views_per_day:
        pace = (views / age_days) / channel_baseline_views_per_day
        components["pace"] = max(0.0, min(pace, 3.0))
    else:
        components["pace"] = None
        missing.append("pace (views/age_days vs channel baseline)")

    subs = metrics.get("subscribers_gained")
    if subs is not None and views:
        components["subscriber"] = min((subs / views) * 1000.0, 1.0)
    else:
        components["subscriber"] = None
        missing.append("subscribers_gained")

    likes = metrics.get("likes")
    comments = metrics.get("comments")
    if likes is not None and comments is not None and views:
        components["engagement"] = min(((likes + comments) / views) * 100.0, 1.0)
    else:
        components["engagement"] = None
        missing.append("likes/comments engagement")

    available = {k: v for k, v in components.items() if v is not None}
    if not available:
        return {"score": None, "components": components, "missing_metrics": missing}

    weight_total = sum(PERFORMANCE_SCORE_WEIGHTS[k] for k in available)
    score = sum(PERFORMANCE_SCORE_WEIGHTS[k] * v for k, v in available.items()) / weight_total

    return {"score": round(score, 4), "components": components, "missing_metrics": missing}


# ══════════════════════════════════════════════════════════════════════════
# RECORDING
# ══════════════════════════════════════════════════════════════════════════

def record_generation(video_hash: str, metadata: dict) -> None:
    """Stores the full metadata for one generated video, keyed by its
    canonical video_hash (see compute_video_hash). Safe to call every
    run."""
    store = _load_store()
    store["videos"][video_hash] = {
        **metadata,
        "video_hash": video_hash,
        "recorded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save_store(store)


def attach_platform_ids(video_hash: str, platform_ids: dict) -> None:
    """
    Called by upload.py right after a successful platform upload to
    attach that platform's video/media ID (e.g. {"youtube_video_id":
    "abc123"}) to the SAME analytics.json record build_video.py already
    created for this video_hash. This is what lets analytics_ingest.py
    later find "videos that have a youtube_video_id but no performance
    numbers yet" without any separate mapping file. Silently does
    nothing (with a log) if the video_hash isn't known — this should
    only happen if analytics.json was reset between generation and
    upload, and must never block the upload itself.
    """
    store = _load_store()
    video = store["videos"].get(video_hash)
    if not video:
        log.warning("attach_platform_ids: unknown video_hash %s — cannot attach %s.", video_hash, platform_ids)
        return
    video.setdefault("platform_ids", {}).update({k: v for k, v in platform_ids.items() if v})
    if any(platform_ids.values()):
        video["uploaded_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save_store(store)


def record_performance(video_hash: str, metrics: dict, channel_baseline_views_per_day: float | None = None) -> dict:
    """
    Feeds real, retrieved performance numbers in for a video previously
    recorded via record_generation() (called by analytics_ingest.py —
    never fabricated, never called with made-up numbers). Computes the
    transparent performance score (compute_performance_score above),
    stores both the raw metrics AND the score breakdown on the video's
    record, and rolls the SCORE up into template_stats[mood][template]
    and duration_stats[bucket] as a running average — but only if a
    score could actually be computed (i.e. at least one real metric was
    available). Returns the score dict for the caller to log/inspect.
    """
    store = _load_store()
    video = store["videos"].get(video_hash)
    if not video:
        log.warning("record_performance: unknown video_hash %s — nothing to attribute this to.", video_hash)
        return {"score": None, "components": {}, "missing_metrics": ["unknown video_hash"]}

    video.setdefault("metrics", {}).update(metrics)
    result = compute_performance_score(metrics, channel_baseline_views_per_day)
    video["performance_score"] = result

    if result["score"] is not None:
        mood = video.get("visual_mood", "reflection")
        template = video.get("visual_template")
        if template:
            mood_stats = store.setdefault("template_stats", {}).setdefault(mood, {})
            _roll_average(mood_stats, template, "avg_score", result["score"])

        bucket = video.get("duration_bucket")
        if bucket:
            duration_stats = store.setdefault("duration_stats", {})
            _roll_average(duration_stats, bucket, "avg_score", result["score"])
    else:
        log.info("No usable metrics for %s yet (missing: %s) — not rolled into template/duration stats.",
                  video_hash[:12], ", ".join(result["missing_metrics"]))

    _save_store(store)
    return result


def _roll_average(stats: dict, key: str, metric_name: str, value: float) -> None:
    entry = stats.setdefault(key, {"count": 0, metric_name: 0.0})
    n = entry.get("count", 0)
    prev_avg = entry.get(metric_name, 0.0)
    entry[metric_name] = (prev_avg * n + value) / (n + 1)
    entry["count"] = n + 1


# ══════════════════════════════════════════════════════════════════════════
# EXTENDED VIDEO METADATA
# ══════════════════════════════════════════════════════════════════════════

def build_metadata(*, surah_num, surah_name, first_ayah, last_ayah, duration,
                    video_file, visual_template, visual_mood, visual_category,
                    motion_styles, transition_style, color_grade,
                    intro_enabled, intro_duration, duration_bucket, title,
                    video_hash: str = "", render_width: int = 0, render_height: int = 0,
                    source_clips: list = None, codec: str = "h264",
                    pixel_format: str = "yuv420p", audio_codec: str = "aac") -> dict:
    """Assembles the full per-video metadata dict described in item 13,
    plus the canonical video_hash so anyone reading video_metadata.json
    directly can see exactly which analytics.json record it corresponds
    to, and a placeholder analytics block making explicit what this
    video does NOT have yet (item 4's target field list) until
    analytics_ingest.py fills it in.

    render_width/render_height/source_clips/codec/pixel_format/audio_codec
    are the 2K-upgrade fields (item 12 of that pass): what resolution
    this video actually rendered at, and — critically — what resolution
    the SOURCE footage actually was per clip, so a viewer of this
    metadata can tell native-2K-or-better footage apart from an
    upscaled 1080p fallback rather than everything just claiming "2K."
    """
    source_clips = source_clips or []
    tiers = [c.get("source_quality_tier", "unknown") for c in source_clips]
    native_2k_or_better = sum(1 for t in tiers if t in ("2160p", "1440p"))
    return {
        "title": title,
        "surah": surah_name,
        "surah_num": surah_num,
        "first_ayah": first_ayah,
        "last_ayah": last_ayah,
        "duration": duration,
        "duration_bucket": duration_bucket,
        "video_file": video_file,
        "video_hash": video_hash,
        "visual_template": visual_template,
        "visual_mood": visual_mood,
        "visual_category": visual_category,
        "motion_styles": motion_styles,
        "transition_style": transition_style,
        "color_grade": color_grade,
        "intro_enabled": intro_enabled,
        "intro_duration": intro_duration,
        "render_width": render_width,
        "render_height": render_height,
        "render_resolution": "2K" if (render_width, render_height) == (1440, 2560) else f"{render_width}x{render_height}",
        "codec": codec,
        "pixel_format": pixel_format,
        "audio_codec": audio_codec,
        "source_clips": source_clips,
        "source_clips_native_2k_or_better": f"{native_2k_or_better}/{len(source_clips)}" if source_clips else "0/0",
        "upload_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "analytics": {
            "youtube_video_id": None,
            "views": None, "likes": None, "comments": None,
            "subscribers_gained": None, "average_view_duration": None,
            "average_percentage_viewed": None, "performance_score": None,
            "note": "Filled in later by analytics_ingest.py once the platform API has data for this video.",
        },
    }
