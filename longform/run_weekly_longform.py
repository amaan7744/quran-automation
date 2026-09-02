#!/usr/bin/env python3
"""
run_weekly_longform.py

Entrypoint for the Friday weekly long-form Quran automation.

The target publication time configured in longform.yml is the YouTube
PUBLICATION time, not the time this script must start. The GitHub Actions
workflow should start sufficiently early to allow rendering and validation
to finish before the target publication time.

Flow:

    select Surah
        ↓
    render
        ↓
    validate
        ↓
    upload privately
        ↓
    persist video_id immediately
        ↓
    configure YouTube scheduled publication
        ↓
    mark completed

Crash safety:

If YouTube upload succeeds but the runner dies before scheduling completes,
the persistent schedule state already contains the YouTube video ID.

The next run therefore:
    - detects the uploaded Surah
    - skips rendering
    - skips re-upload
    - retries scheduling on the existing video

This script does not modify or import from the Shorts/Reels pipeline
intentionally, except for shared infrastructure modules required by the
long-form pipeline.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


# ---------------------------------------------------------------------------
# Long-form configuration
# ---------------------------------------------------------------------------
#
# IMPORTANT:
# `config.py` at repository root is shared configuration used by the
# Shorts/Reels pipeline.
#
# Long-form configuration intentionally lives in:
#
#     longform/longform_config.py
#
# This avoids the Python module-name collision between:
#
#     /config.py
#     /longform/config.py
#
from longform_config import (
    LONGFORM_ENABLED,
    LONGFORM_SCHEDULE_DAY,
    LONGFORM_SCHEDULE_TIME,
    LONGFORM_SCHEDULE_TIMEZONE,
    LONGFORM_SCHEDULE_GRACE_MINUTES,
    LONGFORM_UPLOAD_PRIVACY,
    LONGFORM_SCHEDULE_PUBLISH,
    LONGFORM_WORK_DIR,
)


from quran_metadata import (
    get_surah_info,
    SurahNotFoundError,
)

from surah_builder import (
    build_surah,
    BuildError,
    ScheduleError,
)

from surah_schedule import (
    get_next_surah,
    get_uploaded_video_id,
    mark_pending,
    mark_uploaded,
    mark_completed,
)

from surah_uploader import configure_scheduled_publish
from logging_utils import get_logger


log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

_RFC3339 = "%Y-%m-%dT%H:%M:%SZ"


# ---------------------------------------------------------------------------
# Schedule validation
# ---------------------------------------------------------------------------

def _validate_schedule_config() -> tuple[bool, str]:
    """
    Validate the schedule configuration.

    This intentionally does NOT compare the current time against the target
    publication time.

    The GitHub Actions workflow is expected to start before the target
    publication time because long-form rendering can take hours.
    """

    if LONGFORM_SCHEDULE_DAY not in _WEEKDAYS:
        return (
            False,
            f"schedule.day {LONGFORM_SCHEDULE_DAY!r} "
            f"is not a valid weekday name",
        )

    try:
        hour, minute = (
            int(value)
            for value in LONGFORM_SCHEDULE_TIME.split(":")
        )

        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError

    except ValueError:
        return (
            False,
            f"schedule.time {LONGFORM_SCHEDULE_TIME!r} "
            f"is not a valid HH:MM time",
        )

    if ZoneInfo is not None:
        try:
            ZoneInfo(LONGFORM_SCHEDULE_TIMEZONE)
        except Exception as exc:
            return (
                False,
                f"schedule.timezone "
                f"{LONGFORM_SCHEDULE_TIMEZONE!r} is invalid ({exc})",
            )

    return (
        True,
        (
            f"target publication = "
            f"{LONGFORM_SCHEDULE_DAY} "
            f"{LONGFORM_SCHEDULE_TIME} "
            f"{LONGFORM_SCHEDULE_TIMEZONE}"
        ),
    )


# ---------------------------------------------------------------------------
# Publication-time calculation
# ---------------------------------------------------------------------------

def compute_next_publish_at(
    now: datetime | None = None,
) -> datetime:
    """
    Return the next upcoming configured publication time.

    The returned datetime is timezone-aware and represents the exact
    YouTube `publishAt` target.

    If the configured time has already passed for the current week,
    the target rolls forward to the following week.
    """

    if ZoneInfo is not None:
        target_timezone = ZoneInfo(LONGFORM_SCHEDULE_TIMEZONE)
    else:
        target_timezone = timezone.utc

    if now is None:
        now = datetime.now(target_timezone)
    else:
        now = now.astimezone(target_timezone)

    target_hour, target_minute = (
        int(value)
        for value in LONGFORM_SCHEDULE_TIME.split(":")
    )

    try:
        target_weekday = _WEEKDAYS.index(
            LONGFORM_SCHEDULE_DAY
        )
    except ValueError:
        # Defensive fallback. Validation normally catches this first.
        target_weekday = 4  # Friday

    days_ahead = (
        target_weekday - now.weekday()
    ) % 7

    candidate = (
        now + timedelta(days=days_ahead)
    ).replace(
        hour=target_hour,
        minute=target_minute,
        second=0,
        microsecond=0,
    )

    # If today's target has already passed, use next week's target.
    if candidate <= now:
        candidate += timedelta(days=7)

    return candidate


# ---------------------------------------------------------------------------
# Local build-state helper
# ---------------------------------------------------------------------------

def _uploaded_video_id(
    surah_slug: str,
) -> str | None:
    """
    Read the build's per-Surah state.json and return the YouTube video ID
    only when the UPLOAD stage explicitly reports success.

    This prevents a local `--no-upload` build from being mistaken for a
    successful YouTube upload.

    NOTE:
    This helper is only used after the build itself has completed.

    Crash recovery between GitHub Actions runners relies on the persistent
    schedule state in `surah_schedule.py`, not this local file.
    """

    state_path = (
        LONGFORM_WORK_DIR
        / surah_slug
        / "state.json"
    )

    if not state_path.exists():
        return None

    try:
        state = json.loads(
            state_path.read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return None

    upload_stage = (
        state
        .get("stages", {})
        .get("UPLOAD", {})
    )

    if upload_stage.get("status") != "done":
        return None

    video_id = state.get("video_id")

    if not video_id:
        return None

    return str(video_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Weekly long-form Quran video: "
            "build and schedule the next Surah."
        )
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Run regardless of longform.yml's enabled flag "
            "and skip schedule-config validation."
        ),
    )

    parser.add_argument(
        "--skip-schedule-check",
        action="store_true",
        help=(
            "Skip longform.yml schedule-config validation "
            "but still respect enabled."
        ),
    )

    parser.add_argument(
        "--surah",
        type=int,
        default=None,
        help=(
            "Override sequential selection and build "
            "this specific Surah instead."
        ),
    )

    args = parser.parse_args()

    # -----------------------------------------------------------------------
    # Enabled check
    # -----------------------------------------------------------------------

    if not LONGFORM_ENABLED and not args.force:
        log.info(
            "Long-form pipeline is disabled in longform.yml "
            "(enabled: false) — nothing to do."
        )
        return 0

    # -----------------------------------------------------------------------
    # Schedule configuration validation
    # -----------------------------------------------------------------------

    if not args.force and not args.skip_schedule_check:
        valid, reason = _validate_schedule_config()

        if not valid:
            log.error(
                "longform.yml schedule config is invalid: %s. "
                "Fix longform.yml, or use --force to proceed "
                "with config defaults.",
                reason,
            )
            return 1

        log.info(
            "Schedule config OK: %s.",
            reason,
        )

    # -----------------------------------------------------------------------
    # Calculate target YouTube publication time
    # -----------------------------------------------------------------------

    publish_at_local = compute_next_publish_at()

    publish_at_utc = publish_at_local.astimezone(
        timezone.utc
    )

    publish_at_str = publish_at_utc.strftime(
        _RFC3339
    )

    lead_minutes = (
        publish_at_utc
        - datetime.now(timezone.utc)
    ).total_seconds() / 60.0

    if lead_minutes < LONGFORM_SCHEDULE_GRACE_MINUTES:
        log.warning(
            "Only %.0f minutes remain before the target "
            "publish time (%s %s / %s UTC). "
            "Long-form rendering may not finish in time. "
            "Consider moving the workflow cron earlier.",
            lead_minutes,
            LONGFORM_SCHEDULE_TIME,
            LONGFORM_SCHEDULE_TIMEZONE,
            publish_at_str,
        )
    else:
        log.info(
            "Target YouTube publication: %s %s "
            "(%.1f hours from now, %s UTC).",
            LONGFORM_SCHEDULE_TIME,
            LONGFORM_SCHEDULE_TIMEZONE,
            lead_minutes / 60.0,
            publish_at_str,
        )

    # -----------------------------------------------------------------------
    # Select next Surah
    # -----------------------------------------------------------------------

    surah_num = (
        args.surah
        if args.surah is not None
        else get_next_surah()
    )

    if surah_num is None:
        log.info(
            "Entire Quran has been published and "
            "longform.yml has surah.wrap: false — nothing left to do."
        )
        return 0

    # -----------------------------------------------------------------------
    # Validate Surah
    # -----------------------------------------------------------------------

    try:
        surah_info = get_surah_info(surah_num)

    except SurahNotFoundError as exc:
        log.error(
            "Bad schedule state: %s",
            exc,
        )
        return 1

    # -----------------------------------------------------------------------
    # Crash-safe duplicate-upload recovery
    # -----------------------------------------------------------------------
    #
    # This MUST happen before rendering.
    #
    # If the previous runner successfully uploaded the video but died before
    # scheduling or before completing the state transition, the persistent
    # schedule state contains the video ID.
    #
    # In that case:
    #
    #     DO NOT render again
    #     DO NOT upload again
    #
    # Only retry YouTube scheduling.
    # -----------------------------------------------------------------------

    existing_video_id = get_uploaded_video_id(
        surah_num
    )

    if existing_video_id:
        log.info(
            "Surah %d already has an uploaded YouTube "
            "video (ID %s) awaiting scheduled publication. "
            "Skipping render/upload and retrying scheduling only.",
            surah_num,
            existing_video_id,
        )

        try:
            configure_scheduled_publish(
                existing_video_id,
                publish_at_str,
            )

        except Exception as exc:
            log.error(
                "Retry scheduling failed for Surah %d "
                "(video ID %s): %s. "
                "Schedule state remains 'uploaded' so the "
                "next run can retry without creating a duplicate.",
                surah_num,
                existing_video_id,
                exc,
            )
            return 1

        video_url = (
            f"https://www.youtube.com/watch?v="
            f"{existing_video_id}"
        )

        mark_completed(
            surah_num,
            video_id=existing_video_id,
            video_url=video_url,
        )

        log.info(
            "Weekly run complete: Surah %d scheduled "
            "for publication. %s",
            surah_num,
            video_url,
        )

        return 0

    # -----------------------------------------------------------------------
    # Start new build
    # -----------------------------------------------------------------------

    log.info(
        "Weekly long-form run: Surah %d (%s).",
        surah_num,
        surah_info["name_en"],
    )

    mark_pending(surah_num)

    # -----------------------------------------------------------------------
    # Build / render / validate / upload / schedule
    # -----------------------------------------------------------------------

    try:
        video_path = build_surah(
            surah_num,
            upload=None,
            privacy=LONGFORM_UPLOAD_PRIVACY,
            publish_at=(
                publish_at_str
                if LONGFORM_SCHEDULE_PUBLISH
                else None
            ),

            # CRITICAL CRASH-SAFETY HOOK
            #
            # This executes immediately after YouTube confirms the upload
            # and returns the real video ID.
            #
            # It happens BEFORE build_surah() attempts the SCHEDULE stage.
            #
            # Therefore, if the runner dies immediately after upload,
            # the persistent schedule state already contains:
            #
            #     Surah -> video_id
            #
            # The next weekly run can recover the existing video instead
            # of rendering and uploading a duplicate.
            on_uploaded=lambda video_id: mark_uploaded(
                surah_num,
                video_id=video_id,
            ),
        )

    except ScheduleError as exc:
        # The upload already succeeded because the on_uploaded callback
        # persists the video ID immediately after upload.
        #
        # Keep this explicit safety-net write for compatibility with the
        # existing ScheduleError path.
        log.error(
            "Weekly run: upload succeeded but scheduling "
            "failed for Surah %d (video ID %s): %s. "
            "Schedule state is recorded as 'uploaded'; "
            "the next run will retry scheduling only.",
            surah_num,
            exc.video_id,
            exc,
        )

        mark_uploaded(
            surah_num,
            video_id=exc.video_id,
        )

        return 1

    except BuildError as exc:
        # No successful upload reached the crash-safe callback.
        #
        # Leave the Surah pending so the next run retries the same Surah.
        log.error(
            "Weekly run FAILED for Surah %d: %s. "
            "Schedule state remains 'pending' for retry.",
            surah_num,
            exc,
        )

        return 1

    except Exception as exc:
        # Defensive handling for unexpected errors.
        #
        # Do NOT advance the Surah.
        #
        # If the upload callback already ran, persistent state will already
        # contain the uploaded video ID and the next run can recover it.
        log.exception(
            "Unexpected weekly long-form failure for Surah %d: %s",
            surah_num,
            exc,
        )

        return 1

    # -----------------------------------------------------------------------
    # Confirm upload after successful build
    # -----------------------------------------------------------------------

    video_id = _uploaded_video_id(
        surah_info["slug"]
    )

    if not video_id:
        log.error(
            "Build for Surah %d finished but the UPLOAD stage "
            "did not report a successful YouTube upload. "
            "Leaving schedule state as 'pending' for retry. "
            "Rendered video: %s",
            surah_num,
            video_path,
        )

        return 1

    # -----------------------------------------------------------------------
    # Mark completed
    # -----------------------------------------------------------------------

    video_url = (
        f"https://www.youtube.com/watch?v={video_id}"
    )

    mark_completed(
        surah_num,
        video_id=video_id,
        video_url=video_url,
    )

    log.info(
        "Weekly run complete: Surah %d uploaded and "
        "scheduled for publication. %s",
        surah_num,
        video_url,
    )

    return 0


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(main())
