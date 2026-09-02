#!/usr/bin/env python3
"""
surah_builder.py
Orchestrates one Surah's full build across the tracked stages (spec
section 23):
    DATA -> AUDIO -> TIMELINE -> BACKGROUNDS -> SUBTITLES -> INTRO_OUTRO
    -> THUMBNAIL -> RENDER -> VALIDATION -> UPLOAD

Each stage writes its output to a known path under the Surah's work
directory; a re-run of build_surah() checks for that path before
redoing the work, so a failure at (say) RENDER does not force
re-downloading audio or re-fetching background footage. `force=True`
ignores all of that and rebuilds every stage from scratch.

State is also written to state.json after every stage purely for
human-readable progress inspection — the actual resume mechanism is the
presence of each stage's real output file, which is more robust than a
state flag that could drift out of sync with what's really on disk.
"""

import json
import time
from pathlib import Path
from typing import Callable, Optional

from config import (
    LONGFORM_WORK_DIR, LONGFORM_OUTPUT_DIR, LONGFORM_ARABIC_FONT, LONGFORM_ENGLISH_FONT,
    LONGFORM_INTRO_ENABLED, LONGFORM_OUTRO_ENABLED, LONGFORM_THUMBNAIL_ENABLED,
    LONGFORM_UPLOAD_ENABLED, LONGFORM_UPLOAD_PRIVACY, LONGFORM_PLAYLIST_ID,
    LONGFORM_SCHEDULE_PUBLISH,
    LONGFORM_VIDEO_WIDTH, LONGFORM_VIDEO_HEIGHT,
    QURAN_ARABIC_JSON, QURAN_ENGLISH_JSON,
)
from quran_metadata import get_surah_info
from surah_audio import download_full_surah, build_mastered_audio
from surah_timeline import build_timeline, validate_timeline
from surah_backgrounds import build_landscape_background
from surah_subtitles import build_longform_subtitles, verify_fonts_available, FontNotAvailableError
from surah_intro_outro import build_intro, build_outro
from surah_thumbnail import build_thumbnail
from surah_metadata_gen import build_all_metadata
from surah_renderer import render_final_video
from surah_validator import validate_final_video, ValidationError, quick_media_check
from surah_uploader import upload_surah_video, configure_scheduled_publish
from audio_downloader import get_duration
from logging_utils import get_logger

log = get_logger(__name__)

STAGES = ["DATA", "AUDIO", "TIMELINE", "BACKGROUNDS", "SUBTITLES",
          "INTRO_OUTRO", "THUMBNAIL", "RENDER", "VALIDATION", "UPLOAD", "SCHEDULE"]


class BuildError(Exception):
    pass


class ScheduleError(BuildError):
    """Raised when the YouTube upload itself succeeded but configuring
    scheduled publication (status.publishAt) did not. Carries `video_id` so
    the caller can record it (surah_schedule.mark_uploaded) instead of
    treating this the same as a build that never uploaded anything — that
    distinction is what prevents a retry from creating a duplicate video."""
    def __init__(self, message: str, video_id: str = None):
        super().__init__(message)
        self.video_id = video_id


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_state(state_path: Path, stage: str, status: str, extra: dict = None) -> None:
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
    state.setdefault("stages", {})
    state["stages"][stage] = {"status": status, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if extra:
        state.update(extra)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def build_surah(surah_num: int, *, upload: bool = None, privacy: str = None,
                 force: bool = False, skip_background_download: bool = False,
                 skip_thumbnail: bool = False, publish_at: str = None,
                 on_uploaded: Optional[Callable[[str], None]] = None) -> Path:
    """
    Runs every stage for one Surah. Returns the path to the final video
    file. Raises BuildError on any unrecoverable failure (validation
    failures are never silently swallowed — spec section 27: "DO NOT
    UPLOAD"). Raises the more specific ScheduleError (still a BuildError)
    if upload succeeded but the post-upload SCHEDULE stage did not — see
    that class's docstring.

    `on_uploaded`, if given, is called with the YouTube video_id the
    MOMENT the upload succeeds — before the SCHEDULE stage below is even
    attempted. This is the crash-safety hook: the caller can bind it to
    surah_schedule.mark_uploaded() so "Surah -> video_id" is durably
    recorded in the git-committed weekly schedule state immediately,
    closing the window where the runner dies (OOM, timeout, workflow
    cancellation) between a successful upload and a successful SCHEDULE
    stage. Without it, that crash would leave the persistent schedule
    state at 'pending' with no record the video already exists, and the
    next run would re-render and re-upload the same Surah. Left as an
    injected callback (rather than importing surah_schedule directly here)
    so ad-hoc/manual builds (build_surah.py, build_all_surahs.py) are not
    forced into the weekly sequential schedule state machine — only
    run_weekly_longform.py passes it.

    `publish_at`, if given, is an RFC3339 UTC timestamp (e.g.
    "2026-09-11T13:30:00Z"). When set AND LONGFORM_SCHEDULE_PUBLISH is
    true, the SCHEDULE stage configures YouTube to auto-publish the
    (private) upload at that moment. When None, the SCHEDULE stage is
    skipped entirely and the video is simply left at `privacy` — this is
    the ad-hoc/manual build_surah.py behavior, unchanged.
    """
    if upload is None:
        upload = LONGFORM_UPLOAD_ENABLED
    if privacy is None:
        privacy = LONGFORM_UPLOAD_PRIVACY

    # ── DATA ────────────────────────────────────────────────────────────
    log.info("[STAGE] DATA")
    try:
        verify_fonts_available()
    except FontNotAvailableError as e:
        raise BuildError(str(e)) from e
    surah_info = get_surah_info(surah_num)
    log.info("Building Surah %s (%s) — %d verses, %s",
              surah_info["name_en"], surah_info["name_ar"],
              surah_info["ayah_count"], surah_info["revelation_type"])

    work_dir = LONGFORM_WORK_DIR / surah_info["slug"]
    output_dir = LONGFORM_OUTPUT_DIR / surah_info["slug"]
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = work_dir / "state.json"
    _write_state(state_path, "DATA", "done", {"surah": surah_info})

    arabic_data = _load_json(QURAN_ARABIC_JSON)
    english_data = _load_json(QURAN_ENGLISH_JSON)

    # ── AUDIO ───────────────────────────────────────────────────────────
    log.info("[STAGE] AUDIO")
    try:
        audio_files, durations = download_full_surah(surah_num, surah_info["ayah_count"], work_dir)
        mastered_audio = build_mastered_audio(surah_num, audio_files, work_dir,
                                               expected_duration=sum(durations), force=force)
        audio_duration = get_duration(mastered_audio)
        log.info("Audio duration: %.1fs (%.1f min)", audio_duration, audio_duration / 60)
        _write_state(state_path, "AUDIO", "done")
    except Exception as e:
        _write_state(state_path, "AUDIO", "failed")
        raise BuildError(f"AUDIO stage failed for Surah {surah_num}: {e}") from e

    # ── TIMELINE ────────────────────────────────────────────────────────
    # Always recitation-relative (ayah 1 starts at t=0) — see
    # surah_timeline.py's module docstring for why this must never be
    # built with an intro offset baked in. Built once; nothing about it
    # changes once INTRO_OUTRO runs afterward.
    log.info("[STAGE] TIMELINE")
    try:
        timeline = build_timeline(surah_num, durations, arabic_data, english_data)
        validate_timeline(timeline, surah_info["ayah_count"], audio_duration)
        _write_state(state_path, "TIMELINE", "done")
    except Exception as e:
        _write_state(state_path, "TIMELINE", "failed")
        raise BuildError(f"TIMELINE stage failed for Surah {surah_num}: {e}") from e

    # ── BACKGROUNDS ─────────────────────────────────────────────────────
    log.info("[STAGE] BACKGROUNDS")
    background_path = work_dir / "background.mp4"
    try:
        if skip_background_download and not background_path.exists():
            raise BuildError("--skip-background-download was set but no cached background.mp4 exists.")
        if not skip_background_download:
            build_landscape_background(audio_duration, work_dir, background_path, force=force)
        _write_state(state_path, "BACKGROUNDS", "done")
    except Exception as e:
        _write_state(state_path, "BACKGROUNDS", "failed")
        raise BuildError(f"BACKGROUNDS stage failed for Surah {surah_num}: {e}") from e

    # ── INTRO / OUTRO ───────────────────────────────────────────────────
    # Rendered as their own standalone segments and concatenated in FRONT
    # of / BEHIND the recitation body in surah_renderer.join_segments —
    # that concatenation is what shifts ayah timing forward in the FINAL
    # video. The timeline built above, and the subtitle track burned into
    # the recitation body below, both stay on the recitation's own t=0
    # clock and are never touched by the intro's duration.
    log.info("[STAGE] INTRO_OUTRO")
    intro_path = work_dir / "intro.mp4"
    outro_path = work_dir / "outro.mp4"
    try:
        intro_valid = intro_path.exists() and not force and quick_media_check(
            intro_path, min_duration=0.5, require_video=True, require_audio=True,
            expected_width=LONGFORM_VIDEO_WIDTH, expected_height=LONGFORM_VIDEO_HEIGHT,
        )
        if LONGFORM_INTRO_ENABLED and not intro_valid:
            build_intro(surah_info, intro_path)
        outro_valid = outro_path.exists() and not force and quick_media_check(
            outro_path, min_duration=0.5, require_video=True, require_audio=True,
            expected_width=LONGFORM_VIDEO_WIDTH, expected_height=LONGFORM_VIDEO_HEIGHT,
        )
        if LONGFORM_OUTRO_ENABLED and not outro_valid:
            build_outro(surah_info, outro_path)
        real_intro_duration = get_duration(intro_path) if intro_path.exists() else 0.0
        _write_state(state_path, "INTRO_OUTRO", "done")
    except Exception as e:
        _write_state(state_path, "INTRO_OUTRO", "failed")
        raise BuildError(f"INTRO_OUTRO stage failed for Surah {surah_num}: {e}") from e

    # ── SUBTITLES ───────────────────────────────────────────────────────
    # Burned directly onto the recitation-only background (main_body.mp4
    # in surah_renderer.py), whose own clock starts at t=0 exactly like
    # the timeline above — so this uses the SAME timeline, unmodified.
    log.info("[STAGE] SUBTITLES")
    subtitles_path = work_dir / "subtitles.ass"
    try:
        build_longform_subtitles(timeline, subtitles_path)
        _write_state(state_path, "SUBTITLES", "done")
    except Exception as e:
        _write_state(state_path, "SUBTITLES", "failed")
        raise BuildError(f"SUBTITLES stage failed for Surah {surah_num}: {e}") from e

    # ── THUMBNAIL ───────────────────────────────────────────────────────
    log.info("[STAGE] THUMBNAIL")
    thumbnail_path = output_dir / "thumbnail.jpg"
    try:
        if LONGFORM_THUMBNAIL_ENABLED and not skip_thumbnail and (force or not thumbnail_path.exists()):
            build_thumbnail(background_path, surah_info, thumbnail_path,
                             LONGFORM_ARABIC_FONT, LONGFORM_ENGLISH_FONT)
        _write_state(state_path, "THUMBNAIL", "done")
    except Exception as e:
        _write_state(state_path, "THUMBNAIL", "failed")
        raise BuildError(f"THUMBNAIL stage failed for Surah {surah_num}: {e}") from e

    # ── METADATA (title/description/tags/chapters — not a tracked stage
    #    of its own since it's cheap/pure-python, but written alongside
    #    RENDER for the validator to check) ───────────────────────────────
    metadata_path = output_dir / "metadata.json"
    chapters_path = output_dir / "chapters.txt"
    meta = build_all_metadata(surah_info, timeline, real_intro_duration)
    metadata_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    chapters_path.write_text("\n".join(f"{ts} {label}" for ts, label in meta["chapters"]), encoding="utf-8")

    # ── RENDER ──────────────────────────────────────────────────────────
    log.info("[STAGE] RENDER")
    log.info("Rendering 4K...")
    video_path = output_dir / "video.mp4"
    expected_min_duration = real_intro_duration + audio_duration + (
        get_duration(outro_path) if outro_path.exists() and LONGFORM_OUTRO_ENABLED else 0.0
    )
    try:
        render_final_video(
            background_path, mastered_audio, subtitles_path,
            intro_path if LONGFORM_INTRO_ENABLED else None,
            outro_path if LONGFORM_OUTRO_ENABLED else None,
            work_dir, video_path, min_duration=expected_min_duration, force=force,
        )
        log.info("Rendering complete.")
        _write_state(state_path, "RENDER", "done")
    except Exception as e:
        _write_state(state_path, "RENDER", "failed")
        raise BuildError(f"RENDER stage failed for Surah {surah_num}: {e}") from e

    # ── VALIDATION ──────────────────────────────────────────────────────
    log.info("[STAGE] VALIDATION")
    log.info("Validating output...")
    try:
        validate_final_video(
            video_path, thumbnail_path, metadata_path, timeline,
            surah_info["ayah_count"], expected_min_duration,
            require_thumbnail=(LONGFORM_THUMBNAIL_ENABLED and not skip_thumbnail),
        )
        log.info("Validation passed.")
        _write_state(state_path, "VALIDATION", "done")
    except ValidationError as e:
        _write_state(state_path, "VALIDATION", "failed")
        log.error("Validation failed: %s", e)
        raise BuildError(f"VALIDATION stage failed for Surah {surah_num}: {e}") from e

    # ── UPLOAD ──────────────────────────────────────────────────────────
    if not upload:
        log.info("Upload skipped (--no-upload).")
        _write_state(state_path, "UPLOAD", "skipped")
        return video_path

    log.info("[STAGE] UPLOAD")
    log.info("Uploading to YouTube...")
    try:
        video_id = upload_surah_video(
            video_path, meta["title"], meta["description"], meta["tags"],
            privacy, LONGFORM_PLAYLIST_ID, thumbnail_path,
        )
        log.info("Upload complete.")
        log.info("YouTube video ID: %s", video_id)
        _write_state(state_path, "UPLOAD", "done", {"video_id": video_id})
    except Exception as e:
        _write_state(state_path, "UPLOAD", "failed")
        raise BuildError(f"UPLOAD stage failed for Surah {surah_num}: {e}") from e

    # ── DURABLE UPLOAD RECORD (crash-safe) ────────────────────────────────
    # The upload has ALREADY succeeded at this point — deliberately kept
    # OUTSIDE the try/except above so a failure here can never be
    # mislabeled as "UPLOAD stage failed" (which would leave the
    # persistent schedule state at 'pending' and risk a duplicate upload
    # on retry, exactly backwards from the point of this hook). See
    # `on_uploaded`'s docstring above for why this must happen now, before
    # the SCHEDULE stage below, rather than after it succeeds or fails.
    if on_uploaded is not None:
        on_uploaded(video_id)

    # ── SCHEDULE ────────────────────────────────────────────────────────
    # Upload above always leaves the video at `privacy` (private, in the
    # normal weekly flow) with no publish date. This separate stage
    # configures YouTube's own scheduled-publish so the video goes public
    # automatically at `publish_at` without any runner needing to stay
    # alive until then. Kept as its own try/except (not folded into
    # UPLOAD) so "upload succeeded, scheduling failed" is a distinguishable
    # outcome the caller can recover from safely (spec: duplicate safety).
    if publish_at and LONGFORM_SCHEDULE_PUBLISH:
        log.info("[STAGE] SCHEDULE")
        log.info("Configuring scheduled publish for %s...", publish_at)
        try:
            configure_scheduled_publish(video_id, publish_at)
            log.info("Scheduled publish configured.")
            _write_state(state_path, "SCHEDULE", "done", {"scheduled_publish_at": publish_at})
        except Exception as e:
            _write_state(state_path, "SCHEDULE", "failed")
            raise ScheduleError(
                f"SCHEDULE stage failed for Surah {surah_num} (video {video_id} was uploaded "
                f"successfully): {e}", video_id=video_id,
            ) from e
    else:
        log.info("Scheduled publish not configured (publish_at not set or schedule_publish: false) — "
                  "video remains at privacy=%s with no publish date.", privacy)
        _write_state(state_path, "SCHEDULE", "skipped")

    return video_path
