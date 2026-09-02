#!/usr/bin/env python3
"""
surah_schedule.py
Persistent state for the weekly long-form automation's sequential Surah
selection (spec sections 4-5). Deliberately separate from
longform_video_metadata.json (surah_uploader.py's file-hash upload dedup
history) — that file answers "has this exact rendered file been uploaded
before", this one answers "which Surah is next, and did the last one
actually finish". Different questions, so not a duplicate state system.

State machine (all transitions go through this module — nothing else
writes this file):
    idle -> pending(surah) -> uploaded(surah, video_id) -> completed(surah, video_id)
                                     ^
                                     └── upload succeeded, scheduled-publish
                                         configuration did not (yet)
The "next Surah" is always: the last COMPLETED surah's successor, or
LONGFORM_SURAH_START if nothing has ever completed. A Surah stuck in
`pending` OR `uploaded` (build/upload/scheduling started but never fully
finished) is returned again by get_next_surah() until it either completes
or is explicitly abandoned — this is exactly what spec section 4 requires:
"if a video fails before successful upload, do NOT silently advance to the
next Surah."

The `uploaded` state exists because the long-form workflow now runs on a
GitHub Actions runner that can be completely fresh between attempts (see
run_weekly_longform.py) — the locally rendered MP4 does not necessarily
survive between runs, so the file-hash dedup in surah_uploader.py cannot be
the only defense against a duplicate upload. `uploaded` records the real
YouTube video_id for the Surah currently in flight, in this same
git-committed state file, so a retry can detect "this Surah's video already
exists on YouTube" and skip straight to (re)configuring scheduled
publication instead of re-rendering and re-uploading.
"""

import json
import time
from pathlib import Path

from config import LONGFORM_SCHEDULE_STATE_FILE, LONGFORM_SURAH_START, LONGFORM_SURAH_WRAP
from quran_metadata import all_surah_numbers
from logging_utils import get_logger

log = get_logger(__name__)

_ALL_SURAHS = sorted(all_surah_numbers())


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_state(state_file: Path = LONGFORM_SCHEDULE_STATE_FILE) -> dict:
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("Schedule state file unreadable (%s) — treating as empty.", e)
    return {}


def _save_state(state: dict, state_file: Path = LONGFORM_SCHEDULE_STATE_FILE) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _next_after(surah_num: int) -> int | None:
    """Next Surah number after `surah_num` in the fixed 1-114 order, or
    None if we've reached the end and wrapping is disabled."""
    try:
        idx = _ALL_SURAHS.index(surah_num)
    except ValueError:
        return LONGFORM_SURAH_START
    if idx + 1 < len(_ALL_SURAHS):
        return _ALL_SURAHS[idx + 1]
    return _ALL_SURAHS[0] if LONGFORM_SURAH_WRAP else None


def get_next_surah(state_file: Path = LONGFORM_SCHEDULE_STATE_FILE) -> int | None:
    """
    Returns the Surah number the next weekly run should build.

    - No state yet -> LONGFORM_SURAH_START.
    - Last attempt is still `pending` (previous run failed before a
      successful upload) -> the SAME Surah again, so a retry never skips
      an unpublished one.
    - Last attempt `completed` -> the next Surah in sequence.
    - Returns None only if the whole Quran has been published and
      `surah.wrap: false` in longform.yml — i.e. "nothing left to do".
    """
    state = load_state(state_file)
    status = state.get("status")

    if status in ("pending", "uploaded"):
        surah = state.get("surah")
        log.info("Previous run left Surah %s in status %r (not completed) — retrying it, not advancing.",
                  surah, status)
        return surah

    if status == "completed":
        return _next_after(state["surah"])

    log.info("No prior long-form schedule state — starting from Surah %d.", LONGFORM_SURAH_START)
    return LONGFORM_SURAH_START


def get_uploaded_video_id(surah_num: int, state_file: Path = LONGFORM_SCHEDULE_STATE_FILE) -> str | None:
    """Returns the YouTube video ID already uploaded for `surah_num` if a
    prior run reached `mark_uploaded()` for THIS Surah but never reached
    `mark_completed()` (i.e. upload succeeded, scheduled-publish
    configuration did not) — signaling that a retry should skip straight to
    (re)configuring scheduling rather than re-rendering/re-uploading.
    Returns None otherwise (nothing to resume, or it belongs to a
    different, no-longer-current Surah)."""
    state = load_state(state_file)
    if state.get("status") == "uploaded" and state.get("surah") == surah_num:
        return state.get("video_id")
    return None


def mark_pending(surah_num: int, state_file: Path = LONGFORM_SCHEDULE_STATE_FILE) -> None:
    """Call BEFORE starting a fresh build, so a crash mid-run leaves an
    honest 'pending' record instead of silently looking like nothing
    happened. If this is a genuinely NEW Surah (different from whatever the
    state file currently holds), any leftover video_id/video_url from a
    previous Surah's in-flight upload is cleared first — otherwise it could
    be mistaken for THIS Surah's video by get_uploaded_video_id()."""
    state = load_state(state_file)
    if state.get("surah") != surah_num:
        state["video_id"] = None
        state["video_url"] = None
    state.update({"status": "pending", "surah": surah_num, "started_at": _now()})
    state.setdefault("history", [])
    _save_state(state, state_file)


def mark_uploaded(surah_num: int, video_id: str,
                   state_file: Path = LONGFORM_SCHEDULE_STATE_FILE) -> None:
    """Call after the YouTube upload succeeds but BEFORE scheduled
    publication has been successfully configured. Deliberately a distinct
    status from 'completed' (spec: "DO NOT falsely mark the weekly state
    completed") — get_next_surah() treats this the same as 'pending' (retry
    the same Surah), while get_uploaded_video_id() lets that retry find the
    existing video instead of uploading a duplicate."""
    state = load_state(state_file)
    state.update({"status": "uploaded", "surah": surah_num, "video_id": video_id, "uploaded_at": _now()})
    _save_state(state, state_file)
    log.info("Schedule state: Surah %d marked uploaded (video ID %s), awaiting scheduled publish.",
              surah_num, video_id)


def mark_completed(surah_num: int, video_id: str = None, video_url: str = None,
                    state_file: Path = LONGFORM_SCHEDULE_STATE_FILE) -> None:
    """Call ONLY after render + validation + YouTube upload + scheduled
    publication configuration have all succeeded (spec section 5)."""
    state = load_state(state_file)
    entry = {"surah": surah_num, "video_id": video_id, "video_url": video_url, "completed_at": _now()}
    state.update({"status": "completed", "surah": surah_num, "video_id": video_id,
                  "video_url": video_url, "completed_at": entry["completed_at"]})
    history = state.setdefault("history", [])
    history.append(entry)
    _save_state(state, state_file)
    log.info("Schedule state: Surah %d marked completed. Next Surah will be %s.",
              surah_num, _next_after(surah_num))
