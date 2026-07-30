#!/usr/bin/env python3
"""
upload.py
Publishes the rendered video to YouTube Shorts, Facebook Reels, and
Instagram Reels. Adds retry-with-backoff around every network call,
a persistent upload history to prevent duplicate publishes, generated
captions/hashtags, and structured logging.
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests

from config import (
    GRAPH_VERSION, MAX_UPLOAD_RETRIES, RETRY_BACKOFF_BASE_SECONDS,
    HASHTAG_POOL, UPLOAD_HISTORY_FILE,
)
from logging_utils import get_logger

log = get_logger(__name__)

YOUTUBE_CLIENT_ID     = os.environ.get("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")

# Must be a PAGE ACCESS TOKEN with publish_video permission
META_ACCESS_TOKEN     = os.environ.get("META_ACCESS_TOKEN", "")
FACEBOOK_PAGE_ID      = os.environ.get("FACEBOOK_PAGE_ID", "")
INSTAGRAM_ACCOUNT_ID  = os.environ.get("INSTAGRAM_ACCOUNT_ID", "")

METADATA_FILE = Path("video_metadata.json")
GRAPH_BASE    = f"https://graph.facebook.com/{GRAPH_VERSION}"


# ══════════════════════════════════════════════════════════════════════════
# UPLOAD HISTORY / DEDUPE
# ══════════════════════════════════════════════════════════════════════════

def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_history() -> dict:
    if UPLOAD_HISTORY_FILE.exists():
        try:
            return json.loads(UPLOAD_HISTORY_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("Upload history corrupted — starting fresh.")
    return {"uploads": []}


def save_history(history: dict) -> None:
    UPLOAD_HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def already_uploaded(history: dict, video_hash: str) -> bool:
    return any(entry.get("hash") == video_hash for entry in history["uploads"])


def record_upload(history: dict, video_hash: str, meta: dict, results: dict) -> None:
    history["uploads"].append({
        "hash": video_hash,
        "title": meta.get("title"),
        "surah_num": meta.get("surah_num"),
        "last_ayah": meta.get("last_ayah"),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": results,
    })
    save_history(history)


# ══════════════════════════════════════════════════════════════════════════
# RETRY WRAPPER
# ══════════════════════════════════════════════════════════════════════════

def with_retries(name: str, fn, *args, **kwargs):
    last_error = None
    for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except (requests.RequestException, RuntimeError) as e:
            last_error = e
            wait = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("%s failed (attempt %d/%d): %s — retrying in %ds",
                        name, attempt, MAX_UPLOAD_RETRIES, e, wait)
            if attempt < MAX_UPLOAD_RETRIES:
                time.sleep(wait)
    log.error("%s failed after %d attempts: %s", name, MAX_UPLOAD_RETRIES, last_error)
    return None


# ══════════════════════════════════════════════════════════════════════════
# CAPTION / HASHTAG GENERATION
# ══════════════════════════════════════════════════════════════════════════

def generate_caption(meta: dict) -> str:
    title = meta.get("title", "Quran Recitation")
    surah = meta.get("surah_name", "")
    first, last = meta.get("first_ayah"), meta.get("last_ayah")
    verse_range = f"Ayah {first}-{last}" if first and last and first != last else f"Ayah {last}"

    tags = " ".join(HASHTAG_POOL[:10])
    return f"{title}\n\nSurah {surah} | {verse_range}\n\n{tags}"


# ══════════════════════════════════════════════════════════════════════════
# YOUTUBE
# ══════════════════════════════════════════════════════════════════════════

def youtube_get_access_token() -> str:
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": YOUTUBE_CLIENT_ID,
        "client_secret": YOUTUBE_CLIENT_SECRET,
        "refresh_token": YOUTUBE_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    return r.json().get("access_token")


def _upload_youtube(video_path: str, title: str, description: str) -> str:
    file_size = os.path.getsize(video_path)
    access_token = youtube_get_access_token()

    init_r = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(file_size),
        },
        json={
            "snippet": {"title": title[:100], "description": description, "categoryId": "29"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
        }, timeout=30)
    init_r.raise_for_status()
    upload_url = init_r.headers.get("Location")
    if not upload_url:
        raise RuntimeError("YouTube did not return a resumable upload URL.")

    with open(video_path, "rb") as f:
        upload_r = requests.put(
            upload_url,
            headers={"Content-Length": str(file_size), "Content-Type": "video/mp4"},
            data=f, timeout=600,
        )
    upload_r.raise_for_status()
    return upload_r.json().get("id", "unknown")


def upload_youtube(video_path: str, title: str, description: str) -> str:
    log.info("Uploading to YouTube Shorts...")
    return with_retries("YouTube upload", _upload_youtube, video_path, title, description)


# ══════════════════════════════════════════════════════════════════════════
# FACEBOOK REELS
# ══════════════════════════════════════════════════════════════════════════

def _upload_facebook(video_path: str, description: str) -> str:
    file_size = os.path.getsize(video_path)

    init_r = requests.post(
        f"{GRAPH_BASE}/{FACEBOOK_PAGE_ID}/video_reels",
        json={"upload_phase": "start", "access_token": META_ACCESS_TOKEN},
        timeout=30)
    init_data = init_r.json()
    video_id = init_data.get("video_id")
    upload_url = init_data.get("upload_url")
    if not video_id or not upload_url:
        raise RuntimeError(f"Facebook init failed: {init_data}")

    with open(video_path, "rb") as f:
        upload_r = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {META_ACCESS_TOKEN}",
                "offset": "0",
                "file_size": str(file_size),
                "Content-Type": "application/octet-stream",
            },
            data=f, timeout=600)
    upload_r.raise_for_status()

    pub_r = requests.post(
        f"{GRAPH_BASE}/{FACEBOOK_PAGE_ID}/video_reels",
        params={
            "access_token": META_ACCESS_TOKEN,
            "video_id": video_id,
            "upload_phase": "finish",
            "video_state": "PUBLISHED",
            "description": description[:2200],
        }, timeout=30)

    if not pub_r.ok:
        raise RuntimeError(f"Facebook publish failed: {pub_r.text}")
    return video_id


def upload_facebook(video_path: str, description: str) -> str:
    log.info("Uploading to Facebook Reels...")
    return with_retries("Facebook upload", _upload_facebook, video_path, description)


# ══════════════════════════════════════════════════════════════════════════
# INSTAGRAM REELS
# ══════════════════════════════════════════════════════════════════════════

def _upload_instagram(video_path: str, description: str) -> str:
    file_size = os.path.getsize(video_path)

    init_r = requests.post(
        f"{GRAPH_BASE}/{INSTAGRAM_ACCOUNT_ID}/media",
        params={
            "media_type": "REELS", "upload_type": "resumable",
            "caption": description[:2200], "access_token": META_ACCESS_TOKEN,
        }, timeout=30)
    init_data = init_r.json()
    container_id = init_data.get("id")
    upload_url = init_data.get("uri")
    if not container_id or not upload_url:
        raise RuntimeError(f"Instagram init failed: {init_data}")

    with open(video_path, "rb") as f:
        upload_r = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {META_ACCESS_TOKEN}",
                "offset": "0",
                "file_size": str(file_size),
                "Content-Type": "application/octet-stream",
            },
            data=f, timeout=600)
    upload_r.raise_for_status()

    for _ in range(18):  # up to 3 minutes
        time.sleep(10)
        status_r = requests.get(f"{GRAPH_BASE}/{container_id}", params={
            "fields": "status_code", "access_token": META_ACCESS_TOKEN
        }, timeout=30).json()
        status = status_r.get("status_code")
        log.info("  Instagram processing status: %s", status)
        if status == "FINISHED":
            break
        if status == "ERROR":
            raise RuntimeError(f"Instagram processing failed: {status_r}")
    else:
        raise RuntimeError("Instagram container never finished processing in time.")

    pub_r = requests.post(f"{GRAPH_BASE}/{INSTAGRAM_ACCOUNT_ID}/media_publish", params={
        "creation_id": container_id, "access_token": META_ACCESS_TOKEN
    }, timeout=30)
    if not pub_r.ok:
        raise RuntimeError(f"Instagram publish failed: {pub_r.text}")
    return pub_r.json().get("id")


def upload_instagram(video_path: str, description: str) -> str:
    log.info("Uploading to Instagram Reels...")
    return with_retries("Instagram upload", _upload_instagram, video_path, description)


# ══════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════

def load_metadata() -> dict:
    if not METADATA_FILE.exists():
        raise FileNotFoundError("video_metadata.json not found.")
    with open(METADATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    try:
        meta = load_metadata()
        video_path = meta.get("video_file", "output_video.mp4")
        if not Path(video_path).exists():
            raise FileNotFoundError(f"{video_path} not found — was build_video.py run first?")

        title = meta.get("title", "Quran Recitation")
        description = generate_caption(meta)

        video_hash = file_hash(Path(video_path))
        history = load_history()
        if already_uploaded(history, video_hash):
            log.warning("This exact video was already uploaded previously — skipping to avoid a duplicate post.")
            sys.exit(0)

        results = {"youtube": None, "facebook": None, "instagram": None}

        if YOUTUBE_REFRESH_TOKEN:
            results["youtube"] = upload_youtube(video_path, title, description)
        else:
            log.info("YouTube credentials not set — skipping.")

        if META_ACCESS_TOKEN and FACEBOOK_PAGE_ID:
            results["facebook"] = upload_facebook(video_path, description)
        else:
            log.info("Facebook credentials not set — skipping.")

        if META_ACCESS_TOKEN and INSTAGRAM_ACCOUNT_ID:
            results["instagram"] = upload_instagram(video_path, description)
        else:
            log.info("Instagram credentials not set — skipping.")

        record_upload(history, video_hash, meta, results)

        log.info("FINAL SUMMARY | YouTube: %s | Facebook: %s | Instagram: %s",
                  results["youtube"] or "FAILED", results["facebook"] or "FAILED",
                  results["instagram"] or "FAILED")

        if not any(results.values()):
            sys.exit(1)  # every configured platform failed — surface as a CI failure

    except Exception as e:  # noqa: BLE001
        log.error("FATAL ERROR: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
