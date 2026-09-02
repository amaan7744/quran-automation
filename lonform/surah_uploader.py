#!/usr/bin/env python3
"""
surah_uploader.py
Long-form YouTube upload. Reuses upload.py's shared primitives exactly as
they are — youtube_get_access_token, _youtube_upload_binary,
_youtube_wait_for_processing, _raise_for_platform_error, with_retries,
validate_video_file, file_hash — and adds ONLY what upload.py's
Shorts-specific _upload_youtube doesn't do: thumbnail upload, playlist
assignment, and a configurable (not hard-coded "public") privacy status.

upload.py itself is NOT modified by this feature — _upload_youtube stays
exactly as the daily Shorts workflow depends on it. A separate history
file is used so long-form dedup/tracking never mixes with Shorts'
video_metadata.json.
"""

import json
import os
from pathlib import Path

import requests

from upload import (
    validate_video_file, youtube_get_access_token, _youtube_upload_binary,
    _youtube_wait_for_processing, _raise_for_platform_error, with_retries,
    file_hash, NonRetryableUploadError,
)
from config import LONGFORM_UPLOAD_CATEGORY, LONGFORM_UPLOAD_HISTORY_FILE
from logging_utils import get_logger

log = get_logger(__name__)

LONGFORM_HISTORY_FILE = LONGFORM_UPLOAD_HISTORY_FILE


def load_longform_history() -> dict:
    if LONGFORM_HISTORY_FILE.exists():
        try:
            return json.loads(LONGFORM_HISTORY_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}


def save_longform_history(history: dict) -> None:
    LONGFORM_HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def _set_thumbnail(video_id: str, thumbnail_path: Path, access_token: str) -> None:
    with open(thumbnail_path, "rb") as f:
        r = requests.post(
            f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={video_id}",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "image/jpeg"},
            data=f.read(), timeout=60,
        )
    if not r.ok:
        # Non-fatal: the video itself uploaded successfully. Log and move
        # on rather than treating a thumbnail failure as an upload failure.
        log.warning("Thumbnail upload failed (video still uploaded): %s", r.text[:300])


def configure_scheduled_publish(video_id: str, publish_at_utc: str, access_token: str = None) -> None:
    """
    Configures YouTube's native scheduled-publish mechanism for an already-
    uploaded (private) video: status.privacyStatus stays "private" and
    status.publishAt is set to `publish_at_utc` (an RFC3339 UTC timestamp,
    e.g. "2026-09-11T13:30:00Z"). YouTube itself flips the video public at
    that moment — nothing needs to stay running until then.

    This is a SEPARATE call from the upload (videos.update, not part of
    videos.insert) so upload success and scheduling success are two
    independently-observable outcomes, matching the pipeline's "upload
    succeeded but scheduling failed" recovery state. Raises on failure;
    callers decide how to persist that partial state (see
    surah_schedule.mark_uploaded).
    """
    if access_token is None:
        access_token = youtube_get_access_token()
    r = requests.put(
        "https://www.googleapis.com/youtube/v3/videos?part=status",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={
            "id": video_id,
            "status": {
                "privacyStatus": "private",
                "publishAt": publish_at_utc,
                "selfDeclaredMadeForKids": False,
            },
        },
        timeout=30,
    )
    if not r.ok:
        _raise_for_platform_error(r, "YouTube scheduled-publish configuration")


def _add_to_playlist(video_id: str, playlist_id: str, access_token: str) -> None:
    if not playlist_id:
        return
    r = requests.post(
        "https://www.googleapis.com/youtube/v3/playlistItems?part=snippet",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
        timeout=30,
    )
    if not r.ok:
        log.warning("Adding to playlist %s failed (video still uploaded): %s", playlist_id, r.text[:300])


def _upload_youtube_longform(video_path: str, title: str, description: str, tags: list,
                              privacy: str, playlist_id: str, thumbnail_path: Path) -> str:
    validate_video_file(Path(video_path))
    file_size = os.path.getsize(video_path)
    access_token = youtube_get_access_token()

    snippet = {"title": title[:100], "description": description[:5000], "categoryId": LONGFORM_UPLOAD_CATEGORY}
    if tags:
        snippet["tags"] = tags

    if privacy not in ("private", "unlisted", "public"):
        raise NonRetryableUploadError(f"Invalid privacy status: {privacy!r}")

    init_r = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(file_size),
        },
        json={
            "snippet": snippet,
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }, timeout=30)

    if not init_r.ok:
        _raise_for_platform_error(init_r, "YouTube long-form resumable upload init")

    upload_url = init_r.headers.get("Location")
    if not upload_url:
        raise NonRetryableUploadError("YouTube did not return a resumable upload URL.")

    upload_result = _youtube_upload_binary(upload_url, video_path, file_size)
    video_id = upload_result.get("id", "unknown")

    _youtube_wait_for_processing(video_id, access_token)

    if thumbnail_path and thumbnail_path.exists():
        _set_thumbnail(video_id, thumbnail_path, access_token)
    if playlist_id:
        _add_to_playlist(video_id, playlist_id, access_token)

    return video_id


def upload_surah_video(video_path: Path, title: str, description: str, tags: list,
                        privacy: str, playlist_id: str, thumbnail_path: Path,
                        skip_if_uploaded: bool = True) -> str:
    """
    Uploads one finished long-form video. Deduplicates against
    longform_video_metadata.json by file hash (mirrors upload.py's Shorts
    dedup, but in its own history file). Returns the YouTube video ID.
    """
    video_hash = file_hash(video_path)
    history = load_longform_history()
    if skip_if_uploaded and video_hash in history:
        existing_id = history[video_hash].get("video_id")
        log.info("This exact file was already uploaded (video ID %s) — skipping re-upload.", existing_id)
        return existing_id

    log.info("Uploading long-form video to YouTube (privacy=%s)...", privacy)
    video_id = with_retries(
        "YouTube long-form upload", _upload_youtube_longform,
        str(video_path), title, description, tags, privacy, playlist_id, thumbnail_path,
    )

    history[video_hash] = {"video_id": video_id, "title": title, "privacy": privacy}
    save_longform_history(history)
    log.info("Upload complete. YouTube video ID: %s", video_id)
    return video_id
