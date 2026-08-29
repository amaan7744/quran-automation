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
import random
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
from performance_metadata import attach_platform_ids

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
FACEBOOK_UNSUPPORTED_FIELD_ERROR_CODE = 100  # "(#100) Tried accessing nonexisting field ..."

# upload_phase=finish only *starts* Meta's assemble/encode/publish pipeline
# (per Meta's own Reels Publishing API docs); a 200 there is not proof the
# Reel actually published. Poll GET /{video_id}?fields=status afterward.
FACEBOOK_STATUS_POLL_ATTEMPTS = 30
FACEBOOK_STATUS_POLL_INTERVAL_SECONDS = 10  # 5 min ceiling

# A video is not servable on YouTube until processingDetails.processingStatus
# leaves "processing", regardless of privacyStatus. Poll videos.list after upload.
YOUTUBE_PROCESSING_POLL_ATTEMPTS = 30
YOUTUBE_PROCESSING_POLL_INTERVAL_SECONDS = 10  # 5 min ceiling; we warn (not fail) past this

# The scope this script's YouTube calls actually require: resumable upload
# AND videos.list read of processingDetails. Documenting it here does NOT
# retroactively grant it to an already-issued YOUTUBE_REFRESH_TOKEN — a
# refresh token only carries the scope(s) that were consented to at the time
# it was issued (e.g. a token minted with only
# "https://www.googleapis.com/auth/youtube.upload" will authenticate fine
# for the upload itself but will get 403 ACCESS_TOKEN_SCOPE_INSUFFICIENT on
# the processingDetails read, exactly as seen in production). If that's the
# case, the fix is a fresh OAuth authorization requesting this scope, which
# yields a NEW refresh token to put in YOUTUBE_REFRESH_TOKEN — not a code
# change in this file.
YOUTUBE_REQUIRED_SCOPE = "https://www.googleapis.com/auth/youtube"

# YouTube result states. "Uploaded" and "verified" are deliberately separate
# axes: a video can be fully uploaded to YouTube (video_id exists) while its
# processing/verification status is still unknown, still pending, or
# unreachable due to an OAuth scope problem. None of those verification
# outcomes are upload failures, and none of them should ever trigger a
# re-upload.
YOUTUBE_STATUS_VERIFIED = "uploaded_verified"                          # processingStatus == succeeded
YOUTUBE_STATUS_PROCESSING = "uploaded_processing"                      # still processing at poll ceiling
YOUTUBE_STATUS_VERIFICATION_SCOPE_ERROR = "uploaded_verification_scope_error"  # 403 ACCESS_TOKEN_SCOPE_INSUFFICIENT
YOUTUBE_STATUS_PROCESSING_FAILED = "uploaded_processing_failed"        # processingStatus == failed
YOUTUBE_STATUS_FAILED = "failed"                                       # upload itself never produced a video_id


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


def facebook_get_token_type() -> tuple:
    """
    Determines whether META_ACCESS_TOKEN is a User Access Token or a Page
    Access Token, and returns (token_type, owner_id, owner_name) where
    token_type is "user" or "page".

    This relies on two Graph API facts:
      - GET /me?fields=id,name is valid for BOTH User and Page tokens.
        Critically, a Page token's /me does NOT return the admin user who
        generated it — it resolves to the Page itself (id/name of the
        Page). A User token's /me resolves to the user's own id/name.
      - "category" is only a valid field on Page objects, never on User
        objects. Requesting an invalid field is NOT silently ignored by
        Graph — it raises a hard error: (#100) "Tried accessing
        nonexisting field (category)". Requesting it directly on /me
        (as this function used to do) therefore hard-fails for User
        tokens instead of just telling us "this isn't a Page".

    The fix: never request "category" on /me. Instead, resolve /me's own
    id first, then query THAT id's node for "category". For a Page token,
    that node IS the Page object, so the field is valid and succeeds. For
    a User token, that node is the User object, so the field is invalid
    and fails with the specific #100 "nonexisting field" error — which we
    treat as a positive, expected signal that this is a User token, not
    an unrelated failure.
    """
    me_r = requests.get(f"{GRAPH_BASE}/me", params={
        "fields": "id,name",
        "access_token": META_ACCESS_TOKEN,
    }, timeout=15)
    if not me_r.ok:
        _raise_for_platform_error(me_r, "Facebook token introspection (/me)")

    me_data = me_r.json()
    owner_id = me_data.get("id")
    owner_name = me_data.get("name")
    if not owner_id:
        raise NonRetryableUploadError("Facebook token introspection (/me) returned no id.")

    log.info("Facebook token introspection: /me resolved to id=%s, name=%s", owner_id, owner_name)

    # Query the resolved node (not /me) for "category" — valid only on
    # Page objects. This is the "query the Page object" step.
    cat_r = requests.get(f"{GRAPH_BASE}/{owner_id}", params={
        "fields": "category",
        "access_token": META_ACCESS_TOKEN,
    }, timeout=15)

    if cat_r.ok:
        log.info(
            "Facebook token introspection: node %s exposes 'category' -> "
            "META_ACCESS_TOKEN is a Page Access Token for '%s' (id=%s).",
            owner_id, owner_name, owner_id,
        )
        return "page", owner_id, owner_name

    try:
        err_body = cat_r.json()
    except ValueError:
        err_body = {}
    err_obj = err_body.get("error", {}) if isinstance(err_body, dict) else {}
    err_code = err_obj.get("code")
    err_message = (err_obj.get("message") or "")

    if err_code == FACEBOOK_UNSUPPORTED_FIELD_ERROR_CODE and "nonexisting field" in err_message.lower():
        log.info(
            "Facebook token introspection: node %s has no 'category' field (%s) -> "
            "META_ACCESS_TOKEN is a User Access Token for '%s' (id=%s).",
            owner_id, err_message, owner_name, owner_id,
        )
        return "user", owner_id, owner_name

    # Any other failure (permission error, expired token, transient 5xx,
    # etc.) is a real problem we can't safely classify as "just a User
    # token" — surface it through the normal error path.
    _raise_for_platform_error(cat_r, f"Facebook token introspection (category check on {owner_id})")


def validate_facebook_setup() -> None:
    require_env_vars({"META_ACCESS_TOKEN": META_ACCESS_TOKEN, "FACEBOOK_PAGE_ID": FACEBOOK_PAGE_ID})

    token_type, owner_id, owner_name = facebook_get_token_type()

    if token_type == "user":
        raise NonRetryableUploadError(
            f"META_ACCESS_TOKEN is a User Access Token (user '{owner_name}', id={owner_id}). "
            "Facebook Reels publishing requires a Page Access Token issued for the target "
            "Page itself — a User token cannot publish Reels regardless of which permissions "
            "it carries. Generate a Page Access Token for the configured Page (e.g. via "
            "GET /{page-id}?fields=access_token using a User token that administers the "
            "Page, or the Graph API Explorer's 'Page Access Token' option) and grant it the "
            "pages_manage_posts and publish_video permissions."
        )

    # token_type == "page": this confirms META_ACCESS_TOKEN is *a* Page
    # token, but not necessarily for the *configured* Page — check that
    # explicitly before trusting it for uploads.
    if owner_id != FACEBOOK_PAGE_ID:
        raise NonRetryableUploadError(
            f"META_ACCESS_TOKEN is a valid Page Access Token, but for Page '{owner_name}' "
            f"(id={owner_id}) — not the configured FACEBOOK_PAGE_ID={FACEBOOK_PAGE_ID}. "
            "Generate/set a Page Access Token issued specifically for the configured Page."
        )

    page_r = requests.get(f"{GRAPH_BASE}/{FACEBOOK_PAGE_ID}", params={
        "fields": "id,name", "access_token": META_ACCESS_TOKEN,
    }, timeout=15)
    if not page_r.ok:
        _raise_for_platform_error(page_r, f"Facebook Page validation ({FACEBOOK_PAGE_ID})")

    log.info("Facebook token validated: Page Access Token for '%s' (id=%s) matches configured Page.",
              owner_name, owner_id)


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


def record_upload(history: dict, video_hash: str, meta: dict, results: dict, upload_state: str) -> None:
    history["uploads"].append({
        "hash": video_hash,
        "title": meta.get("title"),
        "surah_num": meta.get("surah_num"),
        "last_ayah": meta.get("last_ayah"),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": results,
        "upload_state": upload_state,
    })
    save_history(history)


# ══════════════════════════════════════════════════════════════════════════
# UPLOAD STATE (item 16: generated / partially_uploaded / fully_uploaded / ...)
# ══════════════════════════════════════════════════════════════════════════

def compute_upload_state(results: dict, configured: list) -> str:
    """
    Reduces per-platform results into one of the states from item 16:
    "generated" (no platform was even configured this run — nothing to
    upload), "upload_failed" (every configured platform failed),
    "partially_uploaded" (some but not all configured platforms
    succeeded), or "fully_uploaded" (every configured platform
    succeeded). Per-platform detail (which of youtube/facebook/
    instagram actually succeeded) is preserved separately in `results`
    itself — this state is just the roll-up summary.
    """
    if not configured:
        return "generated"
    succeeded = [p for p in configured if results.get(p)]
    if not succeeded:
        return "upload_failed"
    if len(succeeded) == len(configured):
        return "fully_uploaded"
    return "partially_uploaded"


def update_metadata_upload_state(metadata_path: Path, meta: dict, results: dict, upload_state: str) -> None:
    """
    Writes the upload outcome back into video_metadata.json so it's
    visible alongside the rest of a video's metadata (item 13/16),
    without touching any of the fields build_video.py already wrote
    (surah/ayah/visual template/etc). Failure to write this is logged
    but never fatal — it must not turn a real upload success into a
    reported script failure.
    """
    try:
        meta["upload_state"] = upload_state
        meta["upload_results"] = {
            "youtube": bool(results.get("youtube")),
            "facebook": bool(results.get("facebook")),
            "instagram": bool(results.get("instagram")),
        }
        metadata_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("Could not write upload_state back to %s: %s", metadata_path, e)


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

CAPTION_INTRO_TEMPLATES = [
    "{title}",
    "🎧 {title}",
    "{title} | Quran Recitation",
]


def generate_caption(meta: dict) -> str:
    """
    Builds the caption/description used across all three platforms.

    Previously this always emitted the exact same intro wording and the
    same first 10 hashtags from HASHTAG_POOL in the same order, on every
    single upload — the only characters that ever changed were the
    title/surah/ayah. Identical or near-identical captions and hashtag
    blocks across consecutive posts are a documented negative signal for
    recommendation systems on all three platforms (read as duplicate/
    spammy content). This rotates the intro phrasing and shuffles the
    hashtag selection per upload while keeping the substantive info
    (title, surah, ayah range) intact.
    """
    title = meta.get("title", "Quran Recitation")
    surah = meta.get("surah_name", "")
    first, last = meta.get("first_ayah"), meta.get("last_ayah")
    verse_range = f"Ayah {first}-{last}" if first and last and first != last else f"Ayah {last}"

    intro = random.choice(CAPTION_INTRO_TEMPLATES).format(title=title)

    pool = list(HASHTAG_POOL)
    random.shuffle(pool)
    tags = " ".join(pool[:min(10, len(pool))])

    return f"{intro}\n\nSurah {surah} | {verse_range}\n\n{tags}"


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


def _youtube_query_resume_offset(upload_url: str, file_size: int) -> int:
    """
    Per the documented resumable-upload recovery flow: PUT an empty body
    with Content-Range: bytes */{file_size} to ask YouTube how many bytes
    of the session it has actually received. A 308 with a Range header
    (e.g. "bytes=0-524287") gives the last byte received; we resume at
    the next one. A 308 with no Range header means nothing was received yet.
    """
    r = requests.put(
        upload_url,
        headers={"Content-Range": f"bytes */{file_size}", "Content-Length": "0"},
        timeout=30,
    )
    if r.status_code == 308:
        range_header = r.headers.get("Range")
        return int(range_header.split("-")[-1]) + 1 if range_header else 0
    if r.ok:
        return file_size  # server already has the whole file
    _raise_for_platform_error(r, "YouTube resumable upload offset check")


def _youtube_upload_binary(upload_url: str, video_path: str, file_size: int) -> dict:
    """
    Uploads the video binary to an already-initialized resumable session.

    Previously a single PUT sent the entire file with no recovery: any
    network blip, timeout, or 5xx on a multi-GB file meant restarting the
    whole upload from byte 0, defeating the point of using the resumable
    protocol. This resumes from the last byte YouTube actually received.
    """
    offset = 0
    last_error = None
    for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
        try:
            with open(video_path, "rb") as f:
                f.seek(offset)
                headers = {
                    "Content-Length": str(file_size - offset),
                    "Content-Type": "video/mp4",
                }
                if offset:
                    headers["Content-Range"] = f"bytes {offset}-{file_size - 1}/{file_size}"
                upload_r = requests.put(upload_url, headers=headers, data=f, timeout=600)

            if upload_r.ok:
                return upload_r.json()
            if upload_r.status_code in TRANSIENT_HTTP_STATUSES:
                raise TransientUploadError(f"YouTube binary upload transient error (HTTP {upload_r.status_code})")
            _raise_for_platform_error(upload_r, "YouTube video binary upload")

        except (TransientUploadError, requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            if attempt >= MAX_UPLOAD_RETRIES:
                break
            wait = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning(
                "YouTube binary upload interrupted (attempt %d/%d): %s — resuming in %ds",
                attempt, MAX_UPLOAD_RETRIES, e, wait,
            )
            time.sleep(wait)
            offset = _youtube_query_resume_offset(upload_url, file_size)
            log.info("YouTube upload resuming from byte %d/%d", offset, file_size)

    raise TransientUploadError(f"YouTube binary upload failed after {MAX_UPLOAD_RETRIES} attempts: {last_error}")


def _youtube_wait_for_processing(video_id: str, access_token: str) -> None:
    """
    A video is not servable on YouTube until processingDetails.processingStatus
    leaves "processing" — this is true even for privacyStatus=public. Without
    this check, the uploader declared success (and recorded it in history) the
    moment the binary upload finished, with no idea whether YouTube's encode
    pipeline subsequently failed the video.
    """
    if video_id == "unknown":
        return
    last_status = "processing"
    for _ in range(YOUTUBE_PROCESSING_POLL_ATTEMPTS):
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"part": "processingDetails", "id": video_id},
            timeout=30,
        )
        if not r.ok:
            _raise_for_platform_error(r, "YouTube processing status check")

        items = r.json().get("items", [])
        if not items:
            raise TransientUploadError(f"YouTube processing status check returned no items for video {video_id}")

        last_status = items[0].get("processingDetails", {}).get("processingStatus", "processing")
        log.info("  YouTube processing status: %s", last_status)

        if last_status == "succeeded":
            return
        if last_status == "failed":
            raise TransientUploadError(f"YouTube reported processing failure for video {video_id}")

        time.sleep(YOUTUBE_PROCESSING_POLL_INTERVAL_SECONDS)

    # Long/large videos can legitimately still be processing past our
    # ceiling — warn rather than fail, so we don't trigger a duplicate
    # re-upload of a video that is actually fine and just slow to encode.
    log.warning(
        "YouTube video %s still '%s' after %ds — not failing the upload, but it may not be fully live yet.",
        video_id, last_status, YOUTUBE_PROCESSING_POLL_ATTEMPTS * YOUTUBE_PROCESSING_POLL_INTERVAL_SECONDS,
    )


def _upload_youtube(video_path: str, title: str, description: str) -> str:
    validate_video_file(Path(video_path))
    file_size = os.path.getsize(video_path)
    access_token = youtube_get_access_token()

    # Keep a #Shorts hint in the title when there's room — still an
    # official signal YouTube documents for routing content to Shorts,
    # even though vertical/short-duration video is now auto-detected too.
    snippet_title = title[:100]
    has_shorts_tag = "#shorts" in snippet_title.lower() or "#shorts" in description.lower()
    if not has_shorts_tag and len(snippet_title) + len(" #Shorts") <= 100:
        snippet_title += " #Shorts"

    snippet = {"title": snippet_title, "description": description, "categoryId": "29"}
    if HASHTAG_POOL:
        # Documented snippet.tags field — was previously left empty,
        # so the only searchable keywords were whatever landed in the
        # description text.
        snippet["tags"] = [t.lstrip("#") for t in HASHTAG_POOL][:15]

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
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
        }, timeout=30)

    if not init_r.ok:
        _raise_for_platform_error(init_r, "YouTube resumable upload init")

    upload_url = init_r.headers.get("Location")
    if not upload_url:
        raise NonRetryableUploadError("YouTube did not return a resumable upload URL.")

    upload_result = _youtube_upload_binary(upload_url, video_path, file_size)
    video_id = upload_result.get("id", "unknown")

    _youtube_wait_for_processing(video_id, access_token)

    return video_id


def upload_youtube(video_path: str, title: str, description: str) -> str:
    log.info("Uploading to YouTube Shorts...")
    return with_retries("YouTube upload", _upload_youtube, video_path, title, description)


# ══════════════════════════════════════════════════════════════════════════
# FACEBOOK REELS
# ══════════════════════════════════════════════════════════════════════════

def _facebook_poll_publish_status(video_id: str) -> dict:
    """
    Polls GET /{video_id}?fields=status until the Reel finishes assembling,
    encoding, and publishing (or errors out).

    THIS IS THE LIKELY ROOT CAUSE of the Facebook view discrepancy. Per
    Meta's own Reels Publishing API reference, upload_phase=finish "ends
    the upload phase to start assembling and encoding the video" — a 200 OK
    on that call only confirms Meta *accepted the request*, not that the
    Reel actually finished processing or went live. The previous code
    treated that 200 as final success: it recorded the post as published
    in upload history and never checked again. A manual upload through the
    Facebook app keeps the UI open and implicitly waits through this same
    assemble/encode/publish pipeline before letting you leave the screen;
    the API path had no equivalent wait, so any processing failure,
    encoding rejection, or incomplete publish after `finish` returned would
    go completely undetected — the video could sit in an errored or
    never-fully-published state indefinitely while this script had already
    logged it as a success. That matches a Reel that gets 0–20 views
    (effectively undistributed) versus the same file, uploaded manually,
    getting normal traffic.
    """
    last_status = {}
    for attempt in range(1, FACEBOOK_STATUS_POLL_ATTEMPTS + 1):
        status_r = requests.get(f"{GRAPH_BASE}/{video_id}", params={
            "fields": "status", "access_token": META_ACCESS_TOKEN,
        }, timeout=30)
        if not status_r.ok:
            _raise_for_platform_error(status_r, "Facebook Reels status check")

        last_status = status_r.json().get("status", {})
        uploading = last_status.get("uploading_phase", {}).get("status")
        processing = last_status.get("processing_phase", {}).get("status")
        publishing = last_status.get("publishing_phase", {}).get("status")
        log.info(
            "  Facebook Reels status (attempt %d/%d): uploading=%s processing=%s publishing=%s",
            attempt, FACEBOOK_STATUS_POLL_ATTEMPTS, uploading, processing, publishing,
        )

        if "error" in (uploading, processing, publishing):
            # Meta-side processing failures are usually transient (encoding
            # queue issues, temporary capacity limits) — let with_retries
            # redo the whole upload rather than silently recording a Reel
            # that never actually published.
            raise TransientUploadError(f"Facebook Reels processing/publishing failed: {last_status}")

        if publishing == "complete":
            return last_status

        time.sleep(FACEBOOK_STATUS_POLL_INTERVAL_SECONDS)

    raise TransientUploadError(
        f"Facebook Reels did not finish processing/publishing within "
        f"{FACEBOOK_STATUS_POLL_ATTEMPTS * FACEBOOK_STATUS_POLL_INTERVAL_SECONDS}s: {last_status}"
    )


def _upload_facebook(video_path: str, title: str, description: str) -> str:
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
            "title": title[:255],
            "description": description[:2200],
        }, timeout=30)

    if not pub_r.ok:
        _raise_for_platform_error(pub_r, "Facebook Reels publish")

    # pub_r.ok only means Meta accepted the finish request and started
    # assembling/encoding — confirm it actually finished publishing before
    # trusting this as a success.
    _facebook_poll_publish_status(video_id)

    return video_id


def upload_facebook(video_path: str, title: str, description: str) -> str:
    log.info("Uploading to Facebook Reels...")
    return with_retries("Facebook upload", _upload_facebook, video_path, title, description)


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
        configured = []

        # Every platform call below is wrapped in its own try/except
        # Exception (not just NonRetryableUploadError) — item 16: if
        # YouTube succeeds but Instagram fails (or vice versa, or
        # either raises something completely unexpected), the OTHER
        # platforms must still be attempted with this SAME rendered
        # video. Nothing here ever triggers a re-render — build_video.py
        # already advanced progress once this file was validated, and
        # that is never revisited based on upload outcome.
        if YOUTUBE_REFRESH_TOKEN:
            configured.append("youtube")
            try:
                results["youtube"] = upload_youtube(video_path, title, description)
            except Exception as e:  # noqa: BLE001 — must not block facebook/instagram below
                log.error("YouTube upload raised an unexpected error (not just failed a retry): %s", e)
        else:
            log.info("YouTube credentials not set — skipping.")

        if META_ACCESS_TOKEN and FACEBOOK_PAGE_ID:
            configured.append("facebook")
            try:
                validate_facebook_setup()
                results["facebook"] = upload_facebook(video_path, title, description)
            except NonRetryableUploadError as e:
                log.error("Facebook upload skipped: %s", e)
            except Exception as e:  # noqa: BLE001 — must not block instagram below
                log.error("Facebook upload raised an unexpected error: %s", e)
        else:
            log.info("Facebook credentials not set — skipping.")

        if META_ACCESS_TOKEN and INSTAGRAM_ACCOUNT_ID:
            configured.append("instagram")
            try:
                validate_instagram_setup()
                results["instagram"] = upload_instagram(video_path, description)
            except NonRetryableUploadError as e:
                log.error("Instagram upload skipped: %s", e)
            except Exception as e:  # noqa: BLE001
                log.error("Instagram upload raised an unexpected error: %s", e)
        else:
            log.info("Instagram credentials not set — skipping.")

        # Attach platform IDs to the SAME analytics.json record
        # build_video.py created for this video_hash (item 4: real
        # analytics feedback loop) — this is what lets
        # analytics_ingest.py later find "uploaded videos with no
        # performance numbers yet" without any separate mapping file.
        # Never blocks/fails the upload itself if analytics.json is
        # unavailable — see attach_platform_ids()'s own error handling.
        try:
            attach_platform_ids(video_hash, {
                "youtube_video_id": results.get("youtube"),
                "facebook_video_id": results.get("facebook"),
                "instagram_media_id": results.get("instagram"),
            })
        except Exception as e:  # noqa: BLE001 — analytics bookkeeping must never block the upload result
            log.warning("Failed to attach platform IDs for analytics: %s", e)

        upload_state = compute_upload_state(results, configured)
        update_metadata_upload_state(METADATA_FILE, meta, results, upload_state)
        record_upload(history, video_hash, meta, results, upload_state)

        log.info("FINAL SUMMARY | YouTube: %s | Facebook: %s | Instagram: %s | upload_state=%s",
                  results["youtube"] or "FAILED", results["facebook"] or "FAILED",
                  results["instagram"] or "FAILED", upload_state)

        if upload_state == "upload_failed":
            sys.exit(1)  # every configured platform failed — surface as a CI failure

    except Exception as e:  # noqa: BLE001
        log.error("FATAL ERROR: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
