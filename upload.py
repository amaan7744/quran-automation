#!/usr/bin/env python3
"""
upload.py
Publishes the rendered video to YouTube Shorts, Facebook Reels, and
Instagram Reels. Adds retry-with-backoff around every network call,
a persistent upload history to prevent duplicate publishes, generated
captions/hashtags, and structured logging.
"""

import json
import hashlib
import os
import re
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

# Must be a PAGE ACCESS TOKEN with publish_video / pages_manage_posts permission
META_ACCESS_TOKEN     = os.environ.get("META_ACCESS_TOKEN", "")
FACEBOOK_PAGE_ID      = os.environ.get("FACEBOOK_PAGE_ID", "")
INSTAGRAM_ACCOUNT_ID  = os.environ.get("INSTAGRAM_ACCOUNT_ID", "")

METADATA_FILE = Path("video_metadata.json")
GRAPH_BASE    = f"https://graph.facebook.com/{GRAPH_VERSION}"

ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}
MAX_VIDEO_SIZE_BYTES = 4 * 1024 * 1024 * 1024  # 4GB practical ceiling for Reels/Shorts

NON_RETRYABLE_OAUTH_ERRORS = {"invalid_grant", "invalid_client", "unauthorized_client", "invalid_request"}
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
TRANSIENT_META_ERROR_CODES = {1, 2}  # Meta's own "unknown/temporary" error codes


# ══════════════════════════════════════════════════════════════════════════
# ERROR TYPES
# ══════════════════════════════════════════════════════════════════════════

class UploadError(Exception):
    """Base class for all upload-related failures."""


class NonRetryableUploadError(UploadError):
    """Auth, permission, or validation failures. Retrying will not help."""


class TransientUploadError(UploadError):
    """Network blips, 5xx responses, or temporary platform processing errors."""


# ══════════════════════════════════════════════════════════════════════════
# LOGGING / REDACTION HELPERS
# ══════════════════════════════════════════════════════════════════════════

_SECRET_PARAM_RE = re.compile(r"(access_token|refresh_token|client_secret)=[^&]+", re.IGNORECASE)


def _redact(text: str) -> str:
    """Strip tokens/secrets out of URLs or bodies before they hit the logs."""
    if not text:
        return text
    return _SECRET_PARAM_RE.sub(r"\1=***REDACTED***", text)


def _raise_for_platform_error(resp: requests.Response, context: str) -> None:
    """
    Log full diagnostic detail for a failing HTTP response (URL, status, body,
    parsed JSON error) with secrets redacted, then raise the correctly
    classified exception (transient vs non-retryable).
    """
    try:
        body_json = resp.json()
    except ValueError:
        body_json = None

    log.error(
        "%s failed | URL: %s | Status: %s | Body: %s",
        context, _redact(resp.url), resp.status_code, _redact(resp.text[:2000]),
    )

    error_obj = {}
    if isinstance(body_json, dict):
        error_obj = body_json.get("error", {}) if isinstance(body_json.get("error"), dict) else {}

    message = (
        error_obj.get("message")
        or (body_json.get("error_description") if isinstance(body_json, dict) else None)
        or resp.text[:500]
    )
    error_code = error_obj.get("code")

    if resp.status_code in TRANSIENT_HTTP_STATUSES or error_code in TRANSIENT_META_ERROR_CODES:
        raise TransientUploadError(f"{context}: transient error (HTTP {resp.status_code}): {message}")

    raise NonRetryableUploadError(f"{context}: {message} (HTTP {resp.status_code}, code={error_code})")


# ══════════════════════════════════════════════════════════════════════════
# VALIDATION
# ══════════════════════════════════════════════════════════════════════════

def require_env_vars(vars_dict: dict) -> None:
    missing = [name for name, value in vars_dict.items() if not value]
    if missing:
        raise NonRetryableUploadError(
            f"Missing required environment variable(s): {', '.join(missing)}"
        )


def validate_video_file(path: Path) -> None:
    if not path.exists():
        raise NonRetryableUploadError(f"{path} not found — was build_video.py run first?")
    if path.suffix.lower() not in ALLOWED_VIDEO_EXTENSIONS:
        raise NonRetryableUploadError(
            f"Unsupported video format '{path.suffix}'. Expected one of {sorted(ALLOWED_VIDEO_EXTENSIONS)}."
        )
    size = path.stat().st_size
    if size == 0:
        raise NonRetryableUploadError(f"{path} is empty (0 bytes) — refusing to upload.")
    if size > MAX_VIDEO_SIZE_BYTES:
        log.warning(
            "%s is %.2f GB, above the typical %.0f GB Reels/Shorts limit — upload may be rejected.",
            path, size / (1024 ** 3), MAX_VIDEO_SIZE_BYTES / (1024 ** 3),
        )


def facebook_get_token_type() -> str:
    """
    Best-effort detection of whether META_ACCESS_TOKEN is a User or a Page token.
    Page tokens return a 'category' field on /me; user tokens do not.
    """
    r = requests.get(f"{GRAPH_BASE}/me", params={
        "fields": "id,name,category",
        "access_token": META_ACCESS_TOKEN,
    }, timeout=15)
    if not r.ok:
        _raise_for_platform_error(r, "Facebook token introspection")
    data = r.json()
    return "page" if "category" in data else "user"


def validate_facebook_setup() -> None:
    require_env_vars({"META_ACCESS_TOKEN": META_ACCESS_TOKEN, "FACEBOOK_PAGE_ID": FACEBOOK_PAGE_ID})

    token_type = facebook_get_token_type()
    if token_type != "page":
        raise NonRetryableUploadError(
            "META_ACCESS_TOKEN is a User Access Token. Facebook Reels publishing requires a "
            "Page Access Token issued for this Page, with the pages_manage_posts and "
            "publish_video permissions granted."
        )

    page_r = requests.get(f"{GRAPH_BASE}/{FACEBOOK_PAGE_ID}", params={
        "fields": "id,name", "access_token": META_ACCESS_TOKEN,
    }, timeout=15)
    if not page_r.ok:
        _raise_for_platform_error(page_r, f"Facebook Page validation ({FACEBOOK_PAGE_ID})")


def validate_instagram_setup() -> None:
    require_env_vars({
        "META_ACCESS_TOKEN": META_ACCESS_TOKEN,
        "INSTAGRAM_ACCOUNT_ID": INSTAGRAM_ACCOUNT_ID,
    })
    r = requests.get(f"{GRAPH_BASE}/{INSTAGRAM_ACCOUNT_ID}", params={
        "fields": "id,username", "access_token": META_ACCESS_TOKEN,
    }, timeout=15)
    if not r.ok:
        _raise_for_platform_error(r, f"Instagram account validation ({INSTAGRAM_ACCOUNT_ID})")


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
        except NonRetryableUploadError as e:
            log.error("%s failed with a non-retryable error — not retrying: %s", name, e)
            return None
        except (TransientUploadError, requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            wait = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning("%s failed (attempt %d/%d): %s — retrying in %ds",
                        name, attempt, MAX_UPLOAD_RETRIES, e, wait)
            if attempt < MAX_UPLOAD_RETRIES:
                time.sleep(wait)
        except requests.RequestException as e:
            # Anything else (malformed request, unexpected client error) — don't hammer the API.
            log.error("%s failed with an unexpected, non-retryable request error: %s", name, e)
            return None
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
    require_env_vars({
        "YOUTUBE_CLIENT_ID": YOUTUBE_CLIENT_ID,
        "YOUTUBE_CLIENT_SECRET": YOUTUBE_CLIENT_SECRET,
        "YOUTUBE_REFRESH_TOKEN": YOUTUBE_REFRESH_TOKEN,
    })

    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": YOUTUBE_CLIENT_ID,
        "client_secret": YOUTUBE_CLIENT_SECRET,
        "refresh_token": YOUTUBE_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }, timeout=30)

    if not r.ok:
        try:
            body = r.json()
        except ValueError:
            body = {}
        error_code = body.get("error", "unknown_error")
        error_desc = body.get("error_description", "")

        log.error(
            "YouTube OAuth token request failed | Status: %s | error=%s | error_description=%s | raw_body=%s",
            r.status_code, error_code, error_desc, _redact(r.text[:1000]),
        )

        if r.status_code >= 500:
            raise TransientUploadError(f"YouTube OAuth server error ({r.status_code}): {error_code}")

        if error_code in NON_RETRYABLE_OAUTH_ERRORS or r.status_code == 400:
            raise NonRetryableUploadError(
                f"YouTube OAuth rejected the request ({error_code}): "
                f"{error_desc or 'check YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN'}"
            )

        raise TransientUploadError(f"YouTube OAuth request failed with status {r.status_code}: {error_code}")

    token = r.json().get("access_token")
    if not token:
        raise NonRetryableUploadError("YouTube OAuth response did not include an access_token.")
    return token


def _upload_youtube(video_path: str, title: str, description: str) -> str:
    validate_video_file(Path(video_path))
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

    if not init_r.ok:
        _raise_for_platform_error(init_r, "YouTube resumable upload init")

    upload_url = init_r.headers.get("Location")
    if not upload_url:
        raise NonRetryableUploadError("YouTube did not return a resumable upload URL.")

    with open(video_path, "rb") as f:
        upload_r = requests.put(
            upload_url,
            headers={"Content-Length": str(file_size), "Content-Type": "video/mp4"},
            data=f, timeout=600,
        )
    if not upload_r.ok:
        _raise_for_platform_error(upload_r, "YouTube video binary upload")

    return upload_r.json().get("id", "unknown")


def upload_youtube(video_path: str, title: str, description: str) -> str:
    log.info("Uploading to YouTube Shorts...")
    return with_retries("YouTube upload", _upload_youtube, video_path, title, description)


# ══════════════════════════════════════════════════════════════════════════
# FACEBOOK REELS
# ══════════════════════════════════════════════════════════════════════════

def _upload_facebook(video_path: str, description: str) -> str:
    validate_video_file(Path(video_path))
    file_size = os.path.getsize(video_path)

    init_r = requests.post(
        f"{GRAPH_BASE}/{FACEBOOK_PAGE_ID}/video_reels",
        json={"upload_phase": "start", "access_token": META_ACCESS_TOKEN},
        timeout=30)
    if not init_r.ok:
        _raise_for_platform_error(init_r, "Facebook Reels init")

    init_data = init_r.json()
    video_id = init_data.get("video_id")
    upload_url = init_data.get("upload_url")
    if not video_id or not upload_url:
        raise NonRetryableUploadError(f"Facebook init returned an unexpected payload: {init_data}")

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
    if not upload_r.ok:
        _raise_for_platform_error(upload_r, "Facebook Reels binary upload")

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
        _raise_for_platform_error(pub_r, "Facebook Reels publish")

    return video_id


def upload_facebook(video_path: str, description: str) -> str:
    log.info("Uploading to Facebook Reels...")
    return with_retries("Facebook upload", _upload_facebook, video_path, description)


# ══════════════════════════════════════════════════════════════════════════
# INSTAGRAM REELS
# ══════════════════════════════════════════════════════════════════════════

def _upload_instagram(video_path: str, description: str) -> str:
    validate_video_file(Path(video_path))
    file_size = os.path.getsize(video_path)

    init_r = requests.post(
        f"{GRAPH_BASE}/{INSTAGRAM_ACCOUNT_ID}/media",
        params={
            "media_type": "REELS", "upload_type": "resumable",
            "caption": description[:2200], "access_token": META_ACCESS_TOKEN,
        }, timeout=30)
    if not init_r.ok:
        _raise_for_platform_error(init_r, "Instagram container init")

    init_data = init_r.json()
    container_id = init_data.get("id")
    upload_url = init_data.get("uri")
    if not container_id or not upload_url:
        raise NonRetryableUploadError(f"Instagram init returned an unexpected payload: {init_data}")

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
    if not upload_r.ok:
        _raise_for_platform_error(upload_r, "Instagram binary upload")

    for _ in range(18):  # up to 3 minutes
        time.sleep(10)
        status_resp = requests.get(f"{GRAPH_BASE}/{container_id}", params={
            "fields": "status_code,status", "access_token": META_ACCESS_TOKEN
        }, timeout=30)
        if not status_resp.ok:
            _raise_for_platform_error(status_resp, "Instagram container status check")

        status_r = status_resp.json()
        status = status_r.get("status_code")
        log.info("  Instagram processing status: %s", status)
        if status == "FINISHED":
            break
        if status == "ERROR":
            # Meta-side processing failures are usually transient; let with_retries
            # retry the whole upload rather than failing hard.
            raise TransientUploadError(f"Instagram processing failed: {status_r}")
    else:
        raise TransientUploadError("Instagram container never finished processing in time.")

    pub_r = requests.post(f"{GRAPH_BASE}/{INSTAGRAM_ACCOUNT_ID}/media_publish", params={
        "creation_id": container_id, "access_token": META_ACCESS_TOKEN
    }, timeout=30)
    if not pub_r.ok:
        _raise_for_platform_error(pub_r, "Instagram publish")

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
        validate_video_file(Path(video_path))

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
            try:
                validate_facebook_setup()
                results["facebook"] = upload_facebook(video_path, description)
            except NonRetryableUploadError as e:
                log.error("Facebook upload skipped: %s", e)
        else:
            log.info("Facebook credentials not set — skipping.")

        if META_ACCESS_TOKEN and INSTAGRAM_ACCOUNT_ID:
            try:
                validate_instagram_setup()
                results["instagram"] = upload_instagram(video_path, description)
            except NonRetryableUploadError as e:
                log.error("Instagram upload skipped: %s", e)
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
