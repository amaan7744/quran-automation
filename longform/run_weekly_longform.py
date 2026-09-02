#!/usr/bin/env python3
"""
run_weekly_longform.py
Entrypoint for the Friday weekly long-form automation. Intended to be
invoked by CI (see .github/workflows/longform-weekly.yml) but works
identically from a local shell.

TARGET PUBLICATION TIME vs. WHEN THIS SCRIPT RUNS: these are now two
different things. longform.yml's `schedule:` block (day/time/timezone) is
the target YOUTUBE PUBLICATION time — NOT the time this script itself must
run at. Long-form rendering can take hours, so the workflow starts this
script well before that target (see the cron in longform-weekly.yml). This
script:
  1. Reads longform.yml (via config.py). If `enabled: false`, exits 0
     immediately without touching anything.
  2. Unless --force / --skip-schedule-check, validates that longform.yml's
     `schedule:` block is well-formed (valid weekday name / HH:MM /
     timezone) — it no longer compares the current time against it, since
     the script is expected to run well ahead of the publish time.
  3. Computes the upcoming `schedule.day` `schedule.time` `schedule.timezone`
     as the target YouTube publishAt.
  4. Asks surah_schedule.get_next_surah() which Surah to build — the same
     Surah again if the previous attempt never finished, otherwise the
     next one in sequence.
  5. DUPLICATE-SAFETY SHORTCUT: if a previous run already uploaded a video
     for that Surah but never finished configuring scheduled publication
     (surah_schedule.get_uploaded_video_id()), this run skips straight to
     (re)configuring scheduling on that existing video — it never
     re-renders or re-uploads, since the local rendered file may not have
     survived onto a fresh GitHub Actions runner.
  6. Otherwise, marks the Surah `pending`, runs the existing build_surah()
     pipeline (render -> validate -> upload PRIVATE -> configure scheduled
     publish, unchanged except for the new SCHEDULE stage), and marks it
     `completed` ONLY if upload AND scheduling both succeeded.
  7. On a failure before upload, leaves the schedule state as `pending` (so
     next Friday's run retries the SAME Surah). On a failure AFTER a
     successful upload but during scheduling, records the video ID via
     `uploaded` state instead (so next run only retries scheduling).

This script does not modify, call, or import anything from the Shorts/
Reels pipeline.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python <3.9 fallback, not expected in this repo's CI
    ZoneInfo = None

from config import (
    LONGFORM_ENABLED, LONGFORM_SCHEDULE_DAY, LONGFORM_SCHEDULE_TIME,
    LONGFORM_SCHEDULE_TIMEZONE, LONGFORM_SCHEDULE_GRACE_MINUTES,
    LONGFORM_UPLOAD_PRIVACY, LONGFORM_SCHEDULE_PUBLISH, LONGFORM_WORK_DIR,
)
from quran_metadata import get_surah_info, SurahNotFoundError
from surah_builder import build_surah, BuildError, ScheduleError
from surah_schedule import (
    get_next_surah, get_uploaded_video_id, mark_pending, mark_uploaded, mark_completed,
)
from surah_uploader import configure_scheduled_publish
from logging_utils import get_logger

log = get_logger(__name__)

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_RFC3339 = "%Y-%m-%dT%H:%M:%SZ"


def _validate_schedule_config() -> tuple[bool, str]:
    """Pure sanity check on longform.yml's `schedule:` block. Unlike the
    old runtime check, this does NOT compare against the current time —
    the workflow is expected to start long before the target publish time,
    so "is it Friday 19:00 right now" is no longer the right question."""
    if LONGFORM_SCHEDULE_DAY not in _WEEKDAYS:
        return False, f"schedule.day {LONGFORM_SCHEDULE_DAY!r} is not a valid weekday name"
    try:
        h, m = (int(x) for x in LONGFORM_SCHEDULE_TIME.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
    except ValueError:
        return False, f"schedule.time {LONGFORM_SCHEDULE_TIME!r} is not a valid HH:MM time"
    if ZoneInfo is not None:
        try:
            ZoneInfo(LONGFORM_SCHEDULE_TIMEZONE)
        except Exception as e:
            return False, f"schedule.timezone {LONGFORM_SCHEDULE_TIMEZONE!r} is invalid ({e})"
    return True, (f"target publication = {LONGFORM_SCHEDULE_DAY} {LONGFORM_SCHEDULE_TIME} "
                  f"{LONGFORM_SCHEDULE_TIMEZONE}")


def compute_next_publish_at(now: datetime = None) -> datetime:
    """Returns the next upcoming `schedule.day` `schedule.time` in
    `schedule.timezone` as a tz-aware datetime — this is the YouTube
    publishAt target. Always in the future relative to `now` (rolls to next
    week if today already IS that day/time or later)."""
    tz = ZoneInfo(LONGFORM_SCHEDULE_TIMEZONE) if ZoneInfo is not None else timezone.utc
    now = now.astimezone(tz) if now is not None else datetime.now(tz)
    target_h, target_m = (int(x) for x in LONGFORM_SCHEDULE_TIME.split(":"))
    try:
        target_weekday = _WEEKDAYS.index(LONGFORM_SCHEDULE_DAY)
    except ValueError:
        target_weekday = 4  # Friday, matching the Python-default fallback
    days_ahead = (target_weekday - now.weekday()) % 7
    candidate = (now + timedelta(days=days_ahead)).replace(
        hour=target_h, minute=target_m, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def _uploaded_video_id(surah_slug: str) -> str | None:
    """Reads the build's own per-Surah state.json and returns the YouTube
    video ID ONLY if the UPLOAD stage reported success there — since
    build_surah() also returns normally when upload was intentionally
    skipped (--no-upload), returning None (not just checking truthiness)
    is what tells the caller "no real upload happened, don't advance"."""
    state_path = LONGFORM_WORK_DIR / surah_slug / "state.json"
    if not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    upload_stage = state.get("stages", {}).get("UPLOAD", {})
    if upload_stage.get("status") != "done":
        return None
    return state.get("video_id")


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly long-form Quran video: build + publish the next Surah.")
    parser.add_argument("--force", action="store_true",
                         help="Run regardless of longform.yml's enabled flag and skip schedule-config validation.")
    parser.add_argument("--skip-schedule-check", action="store_true",
                         help="Skip longform.yml schedule-config validation but still respect `enabled`.")
    parser.add_argument("--surah", type=int, default=None,
                         help="Override sequential selection and build this specific Surah instead.")
    args = parser.parse_args()

    if not LONGFORM_ENABLED and not args.force:
        log.info("Long-form pipeline is disabled in longform.yml (enabled: false) — nothing to do.")
        return 0

    if not args.force and not args.skip_schedule_check:
        ok, reason = _validate_schedule_config()
        if not ok:
            log.error("longform.yml schedule config is invalid: %s. Fix longform.yml, or use --force to "
                       "proceed with config.py's built-in defaults.", reason)
            return 1
        log.info("Schedule config OK: %s.", reason)

    publish_at_local = compute_next_publish_at()
    publish_at_utc = publish_at_local.astimezone(timezone.utc)
    publish_at_str = publish_at_utc.strftime(_RFC3339)
    lead_minutes = (publish_at_utc - datetime.now(timezone.utc)).total_seconds() / 60.0
    if lead_minutes < LONGFORM_SCHEDULE_GRACE_MINUTES:
        log.warning("Only %.0f minutes remain before the target publish time (%s %s / %s) — long-form "
                     "rendering may not finish in time. Consider moving the workflow's cron earlier.",
                     lead_minutes, LONGFORM_SCHEDULE_TIME, LONGFORM_SCHEDULE_TIMEZONE, publish_at_str)
    else:
        log.info("Target YouTube publication: %s %s (%.1f hours from now, %s UTC).",
                  LONGFORM_SCHEDULE_TIME, LONGFORM_SCHEDULE_TIMEZONE, lead_minutes / 60.0, publish_at_str)

    surah_num = args.surah if args.surah is not None else get_next_surah()
    if surah_num is None:
        log.info("Entire Quran has been published and longform.yml has surah.wrap: false — nothing left to do.")
        return 0

    try:
        surah_info = get_surah_info(surah_num)
    except SurahNotFoundError as e:
        log.error("Bad schedule state: %s", e)
        return 1

    # ── Duplicate-safety shortcut ──────────────────────────────────────
    # A previous run may have uploaded this exact Surah's video already but
    # never finished configuring scheduled publication (e.g. the runner was
    # lost between GitHub Actions attempts). Recover WITHOUT re-rendering
    # or re-uploading: just (re)try scheduling on the video we already
    # have. This is deliberately checked against the persisted schedule
    # state (survives a fresh runner), not the local MP4 file hash.
    existing_video_id = get_uploaded_video_id(surah_num)
    if existing_video_id:
        log.info("Surah %d already has an uploaded YouTube video (ID %s) awaiting scheduled "
                  "publication — skipping render/upload, only (re)configuring scheduled publish.",
                  surah_num, existing_video_id)
        try:
            configure_scheduled_publish(existing_video_id, publish_at_str)
        except Exception as e:
            log.error("Retry: scheduling still failed for Surah %d (video ID %s): %s. Schedule state "
                       "left as 'uploaded' for another retry — no duplicate upload will be attempted.",
                       surah_num, existing_video_id, e)
            return 1
        video_url = f"https://www.youtube.com/watch?v={existing_video_id}"
        mark_completed(surah_num, video_id=existing_video_id, video_url=video_url)
        log.info("Weekly run complete: Surah %d scheduled for publication. %s", surah_num, video_url)
        return 0

    log.info("Weekly long-form run: Surah %d (%s).", surah_num, surah_info["name_en"])
    mark_pending(surah_num)

    try:
        video_path = build_surah(
            surah_num, upload=None, privacy=LONGFORM_UPLOAD_PRIVACY,
            publish_at=(publish_at_str if LONGFORM_SCHEDULE_PUBLISH else None),
        )
    except ScheduleError as e:
        log.error("Weekly run: upload succeeded but SCHEDULING failed for Surah %d (video ID %s): %s. "
                   "Recording the upload so the next run retries scheduling only — NOT a re-upload.",
                   surah_num, e.video_id, e)
        mark_uploaded(surah_num, video_id=e.video_id)
        return 1
    except BuildError as e:
        log.error("Weekly run FAILED for Surah %d: %s. Schedule state left as 'pending' for retry.",
                   surah_num, e)
        return 1

    video_id = _uploaded_video_id(surah_info["slug"])
    if not video_id:
        log.error("Build for Surah %d finished but UPLOAD stage did not report success — "
                  "leaving schedule state as 'pending' for retry. (Video rendered at: %s)",
                  surah_num, video_path)
        return 1

    video_url = f"https://www.youtube.com/watch?v={video_id}"
    mark_completed(surah_num, video_id=video_id, video_url=video_url)
    log.info("Weekly run complete: Surah %d uploaded and scheduled for publication. %s",
              surah_num, video_url or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
