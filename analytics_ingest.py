#!/usr/bin/env python3
"""
analytics_ingest.py
Item 4 of the final quality pass: the actual analytics feedback loop.

    generated video -> metadata stored (build_video.py)
            -> uploaded, platform ID stored (upload.py)
            -> [THIS SCRIPT] YouTube Data/Analytics API data collected
            -> metrics associated with the exact generated video (by video_hash)
            -> performance score calculated (performance_metadata.compute_performance_score)
            -> rolled into template/duration stats (performance_metadata.record_performance)
            -> future generation probabilities adjusted (performance_metadata.pick_template/pick_duration_bucket)

This is a STANDALONE script, deliberately separate from build_video.py
and upload.py — it's meant to run on its own schedule (e.g. a second,
daily GitHub Actions workflow; see the delivery notes for a suggested
one), since YouTube's own numbers — especially Analytics API metrics
like average view percentage — need time to stabilize after upload,
and re-fetching on every build/upload run would be wasteful and noisy.

HONESTY NOTE (per explicit instruction): this module's HTTP-calling
functions (fetch_public_statistics, fetch_view_analytics) have NOT been
exercised against the real YouTube Data API / YouTube Analytics API in
this environment — this sandbox has no network access to
googleapis.com. What HAS been tested (see the delivery notes) is
everything downstream of the HTTP boundary: response parsing, missing-
scope/missing-field handling, channel-baseline computation, and the
full ingest_pending() orchestration, all exercised against fake/mocked
HTTP responses shaped like real API responses. Treat the live API
integration as implemented-but-unverified until it's run once against
real credentials.

No metric is ever fabricated. Anything the API doesn't return, or that
fails due to a missing OAuth scope, is left as None and flows through
performance_metadata.compute_performance_score() as "unavailable,"
never as a guessed/default value.
"""

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from config import ANALYTICS_FILE, ANALYTICS_MIN_VIDEO_AGE_HOURS, YOUTUBE_CHANNEL_ID
from performance_metadata import record_performance
from upload import youtube_get_access_token, NonRetryableUploadError, TransientUploadError
from logging_utils import get_logger

log = get_logger(__name__)

YOUTUBE_DATA_API = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_ANALYTICS_API = "https://youtubeanalytics.googleapis.com/v2/reports"


# ══════════════════════════════════════════════════════════════════════════
# STORE ACCESS (read-only here — writes go through performance_metadata)
# ══════════════════════════════════════════════════════════════════════════

def _load_store() -> dict:
    if not ANALYTICS_FILE.exists():
        return {"videos": {}, "template_stats": {}, "duration_stats": {}}
    try:
        return json.loads(ANALYTICS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.error("Cannot read %s: %s", ANALYTICS_FILE, e)
        return {"videos": {}, "template_stats": {}, "duration_stats": {}}


def _parse_utc(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def find_pending_videos(store: dict, min_age_hours: float) -> list:
    """
    Videos that were uploaded to YouTube (have a youtube_video_id),
    are old enough for YouTube's own numbers to have stabilized, and
    haven't been scored yet THIS round (re-ingesting is fine/expected —
    performance can be re-fetched periodically to get more mature
    numbers — but a single run only processes each video once).
    """
    now = datetime.now(timezone.utc)
    pending = []
    for video_hash, video in store.get("videos", {}).items():
        yt_id = video.get("platform_ids", {}).get("youtube_video_id")
        if not yt_id:
            continue
        uploaded_at = video.get("uploaded_at_utc")
        if not uploaded_at:
            continue
        try:
            age = now - _parse_utc(uploaded_at)
        except ValueError:
            continue
        if age < timedelta(hours=min_age_hours):
            continue
        pending.append((video_hash, video, yt_id))
    return pending


# ══════════════════════════════════════════════════════════════════════════
# CHANNEL BASELINE (for the "pace" score component — item 6)
# ══════════════════════════════════════════════════════════════════════════

def compute_channel_baseline_views_per_day(store: dict, exclude_hash: str = None) -> float | None:
    """
    Average (views / age_days) across every video in the store that
    already has both numbers on record, excluding the video currently
    being scored (so a video's own performance never biases its own
    baseline comparison). Returns None — not a guessed default — if
    there isn't at least one other video with usable data yet, which
    correctly makes the "pace" score component unavailable for the
    very first videos ingested.
    """
    rates = []
    for video_hash, video in store.get("videos", {}).items():
        if video_hash == exclude_hash:
            continue
        metrics = video.get("metrics", {})
        views = metrics.get("views")
        age_days = metrics.get("age_days")
        if views is not None and age_days and age_days > 0:
            rates.append(views / age_days)
    if not rates:
        return None
    return sum(rates) / len(rates)


# ══════════════════════════════════════════════════════════════════════════
# YOUTUBE DATA API (public stats: views/likes/comments — NOT verified live, see module docstring)
# ══════════════════════════════════════════════════════════════════════════

def fetch_public_statistics(video_id: str, access_token: str, get=requests.get) -> dict:
    """
    GET youtube/v3/videos?part=statistics,snippet&id=<id>. Returns
    {"views": int|None, "likes": int|None, "comments": int|None,
    "age_days": float|None}. Any field YouTube omits (e.g. likeCount/
    commentCount when disabled by the uploader) is left None, never
    defaulted to 0 — a video with comments turned off is "unavailable,"
    not "zero engagement."
    """
    result = {"views": None, "likes": None, "comments": None, "age_days": None}
    try:
        r = get(
            YOUTUBE_DATA_API,
            params={"part": "statistics,snippet", "id": video_id},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
    except requests.RequestException as e:
        log.warning("YouTube Data API request failed for %s: %s", video_id, e)
        return result

    if not r.ok:
        log.warning("YouTube Data API returned %s for %s: %s", r.status_code, video_id, r.text[:300])
        return result

    try:
        items = r.json().get("items", [])
    except ValueError:
        log.warning("YouTube Data API returned non-JSON for %s", video_id)
        return result

    if not items:
        log.warning("YouTube Data API: video %s not found (deleted/private?).", video_id)
        return result

    stats = items[0].get("statistics", {})
    snippet = items[0].get("snippet", {})

    for key, field in (("viewCount", "views"), ("likeCount", "likes"), ("commentCount", "comments")):
        if key in stats:
            try:
                result[field] = int(stats[key])
            except (TypeError, ValueError):
                pass  # leave as None rather than guess

    published_at = snippet.get("publishedAt")
    if published_at:
        try:
            age = datetime.now(timezone.utc) - _parse_utc(published_at)
            result["age_days"] = max(age.total_seconds() / 86400.0, 0.01)
        except ValueError:
            pass

    return result


# ══════════════════════════════════════════════════════════════════════════
# YOUTUBE ANALYTICS API (retention/subs — requires yt-analytics.readonly scope)
# ══════════════════════════════════════════════════════════════════════════

def fetch_view_analytics(video_id: str, channel_id: str, access_token: str, get=requests.get) -> dict:
    """
    GET youtubeAnalytics/v2/reports for averageViewDuration,
    averageViewPercentage, subscribersGained, filtered to this one
    video, over its full lifetime to date. Returns
    {"average_view_duration": float|None, "average_percentage_viewed":
    float|None (0..1), "subscribers_gained": int|None}.

    Requires the OAuth token to carry the
    'https://www.googleapis.com/auth/yt-analytics.readonly' scope —
    the plain upload scope does NOT include this. A 403 here almost
    always means that scope is missing; this is logged once, clearly,
    and all three fields are left unavailable rather than the call
    being retried forever or the script crashing.
    """
    result = {"average_view_duration": None, "average_percentage_viewed": None, "subscribers_gained": None}
    if not channel_id:
        log.info("YOUTUBE_CHANNEL_ID not set — skipping YouTube Analytics API metrics for %s "
                  "(public view/like/comment counts are unaffected).", video_id)
        return result

    try:
        r = get(
            YOUTUBE_ANALYTICS_API,
            params={
                "ids": f"channel=={channel_id}",
                "startDate": "2005-01-01",  # YouTube's own founding year is a safe "since forever" floor
                "endDate": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "metrics": "averageViewDuration,averageViewPercentage,subscribersGained",
                "filters": f"video=={video_id}",
            },
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
    except requests.RequestException as e:
        log.warning("YouTube Analytics API request failed for %s: %s", video_id, e)
        return result

    if r.status_code == 403:
        log.warning(
            "YouTube Analytics API returned 403 for %s — the OAuth token most likely lacks the "
            "'yt-analytics.readonly' scope. Retention/subscriber metrics will stay unavailable "
            "for every video until a token with that scope is issued (see config.py notes).",
            video_id,
        )
        return result
    if not r.ok:
        log.warning("YouTube Analytics API returned %s for %s: %s", r.status_code, video_id, r.text[:300])
        return result

    try:
        body = r.json()
        headers = [h["name"] for h in body.get("columnHeaders", [])]
        rows = body.get("rows", [])
    except (ValueError, KeyError):
        log.warning("YouTube Analytics API returned an unexpected shape for %s", video_id)
        return result

    if not rows:
        log.info("YouTube Analytics API: no rows yet for %s (too new, or genuinely zero views).", video_id)
        return result

    row = dict(zip(headers, rows[0]))
    if "averageViewDuration" in row:
        result["average_view_duration"] = row["averageViewDuration"]
    if "averageViewPercentage" in row:
        # YouTube reports this as a 0-100 percentage; performance_metadata
        # expects a 0..1 fraction to match the "retention" component scale.
        result["average_percentage_viewed"] = row["averageViewPercentage"] / 100.0
    if "subscribersGained" in row:
        result["subscribers_gained"] = row["subscribersGained"]

    return result


# ══════════════════════════════════════════════════════════════════════════
# ORCHESTRATION
# ══════════════════════════════════════════════════════════════════════════

def ingest_pending(min_age_hours: float = ANALYTICS_MIN_VIDEO_AGE_HOURS,
                    get=requests.get, dry_run: bool = False) -> list:
    """
    Finds every uploaded video old enough to have stabilized YouTube
    numbers, fetches whatever real metrics are available for it,
    computes the transparent performance score, and (unless dry_run)
    rolls it into performance_metadata's template/duration stats.
    Returns a list of {"video_hash", "youtube_video_id", "score_result"}
    for logging/inspection — always, even in dry_run mode.

    `get` is injectable so this can be exercised against a fake HTTP
    layer in tests without any real network access — see the delivery
    notes for what was actually tested this way.
    """
    store = _load_store()
    pending = find_pending_videos(store, min_age_hours)
    if not pending:
        log.info("No videos ready for analytics ingestion (need a youtube_video_id and >= %.0fh since upload).",
                  min_age_hours)
        return []

    try:
        access_token = youtube_get_access_token()
    except (NonRetryableUploadError, TransientUploadError) as e:
        log.error("Cannot ingest analytics — YouTube OAuth failed: %s", e)
        return []

    results = []
    for video_hash, video, yt_id in pending:
        log.info("Ingesting analytics for %s (video_hash=%s)", yt_id, video_hash[:12])
        public_stats = fetch_public_statistics(yt_id, access_token, get=get)
        view_analytics = fetch_view_analytics(yt_id, YOUTUBE_CHANNEL_ID, access_token, get=get)
        metrics = {**public_stats, **view_analytics}

        # Update the in-memory snapshot immediately (in addition to the
        # persisted write inside record_performance below) so that if
        # THIS batch processes multiple videos, a later video's channel
        # baseline reflects an earlier video's freshly-fetched metrics
        # rather than a stale pre-batch snapshot — otherwise "performance
        # relative to age" (item 6) would silently use outdated baselines
        # within a single run.
        video.setdefault("metrics", {}).update(metrics)

        baseline = compute_channel_baseline_views_per_day(store, exclude_hash=video_hash)

        if dry_run:
            from performance_metadata import compute_performance_score
            score_result = compute_performance_score(metrics, baseline)
            log.info("[dry-run] %s -> score=%s missing=%s", yt_id, score_result["score"], score_result["missing_metrics"])
        else:
            score_result = record_performance(video_hash, metrics, baseline)
            log.info("%s -> score=%s missing=%s", yt_id, score_result["score"], score_result["missing_metrics"])

        results.append({"video_hash": video_hash, "youtube_video_id": yt_id, "score_result": score_result})

    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="Fetch and score but don't write results into analytics.json")
    parser.add_argument("--min-age-hours", type=float, default=ANALYTICS_MIN_VIDEO_AGE_HOURS,
                         help="Only ingest videos uploaded at least this many hours ago")
    args = parser.parse_args()

    results = ingest_pending(min_age_hours=args.min_age_hours, dry_run=args.dry_run)
    log.info("Analytics ingestion complete: %d video(s) processed.", len(results))


if __name__ == "__main__":
    main()
