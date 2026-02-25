#!/usr/bin/env python3
"""
upload.py
Uploads the built video to:
  1. YouTube Shorts  — via YouTube Data API v3
  2. Facebook Reels  — via Meta Graph API (resumable upload)
  3. Instagram Reels — via Meta Graph API (resumable upload)

Credentials come from GitHub Secrets as environment variables.
"""

import os
import sys
import json
import time
import requests
from pathlib import Path

# ─── CREDENTIALS (GitHub Secrets) ────────────────────────────────────────────
YOUTUBE_CLIENT_ID     = os.environ.get("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")

META_ACCESS_TOKEN     = os.environ.get("META_ACCESS_TOKEN", "")
FACEBOOK_PAGE_ID      = os.environ.get("FACEBOOK_PAGE_ID", "")
INSTAGRAM_ACCOUNT_ID  = os.environ.get("INSTAGRAM_ACCOUNT_ID", "")
# ─────────────────────────────────────────────────────────────────────────────

METADATA_FILE = Path("video_metadata.json")
GRAPH_VERSION = "v20.0"
GRAPH_BASE    = f"https://graph.facebook.com/{GRAPH_VERSION}"


def load_metadata() -> dict:
    if not METADATA_FILE.exists():
        raise FileNotFoundError(
            "video_metadata.json not found. Make sure build_video.py ran successfully."
        )
    with open(METADATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def print_section(title: str) -> None:
    print(f"\n{'─' * 50}")
    print(f"  {title}")
    print(f"{'─' * 50}")


# ══════════════════════════════════════════════════════════════════════════════
# YOUTUBE
# ══════════════════════════════════════════════════════════════════════════════

def youtube_get_access_token() -> str:
    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id":     YOUTUBE_CLIENT_ID,
            "client_secret": YOUTUBE_CLIENT_SECRET,
            "refresh_token": YOUTUBE_REFRESH_TOKEN,
            "grant_type":    "refresh_token",
        },
        timeout=30,
    )
    r.raise_for_status()
    token = r.json().get("access_token")
    if not token:
        raise RuntimeError(f"No access token returned: {r.json()}")
    return token


def upload_youtube(video_path: str, title: str, description: str) -> str:
    print_section("Uploading to YouTube Shorts")

    file_size    = os.path.getsize(video_path)
    access_token = youtube_get_access_token()
    print(f"  File  : {video_path} ({file_size / 1024 / 1024:.1f} MB)")
    print(f"  Title : {title}")

    # Step 1: initiate resumable upload session
    init_r = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos"
        "?uploadType=resumable&part=snippet,status",
        headers={
            "Authorization":           f"Bearer {access_token}",
            "Content-Type":            "application/json; charset=UTF-8",
            "X-Upload-Content-Type":   "video/mp4",
            "X-Upload-Content-Length": str(file_size),
        },
        json={
            "snippet": {
                "title":       title[:100],
                "description": description,
                "tags": [
                    "quran", "quranrecitation", "quranshorts", "islamicvideo",
                    "shorts", "dailyquran", "islam", "qurantilawat",
                    "saudialshuraim", "muslimshorts", "islamicshorts",
                    "quranverses", "islamicreminder", "qurankareem", "allahuakbar",
                ],
                "categoryId": "29",   # Nonprofits & Activism
            },
            "status": {
                "privacyStatus":           "public",
                "selfDeclaredMadeForKids": False,
                "madeForKids":             False,
            },
        },
        timeout=30,
    )
    init_r.raise_for_status()

    upload_url = init_r.headers.get("Location")
    if not upload_url:
        raise RuntimeError("YouTube did not return a resumable upload URL.")

    # Step 2: upload video bytes
    print("  Uploading video bytes...")
    with open(video_path, "rb") as f:
        upload_r = requests.put(
            upload_url,
            headers={
                "Content-Length": str(file_size),
                "Content-Type":   "video/mp4",
            },
            data=f,
            timeout=600,
        )
    upload_r.raise_for_status()

    video_id = upload_r.json().get("id", "unknown")
    print(f"  Done!")
    print(f"  Video ID : {video_id}")
    print(f"  URL      : https://www.youtube.com/shorts/{video_id}")
    return video_id


# ══════════════════════════════════════════════════════════════════════════════
# FACEBOOK REELS
# ══════════════════════════════════════════════════════════════════════════════

def upload_facebook(video_path: str, title: str, description: str) -> str:
    print_section("Uploading to Facebook Reels")

    file_size = os.path.getsize(video_path)
    print(f"  File  : {video_path} ({file_size / 1024 / 1024:.1f} MB)")

    # Step 1: start upload session
    print("  Step 1: Starting upload session...")
    init_r = requests.post(
        f"{GRAPH_BASE}/{FACEBOOK_PAGE_ID}/video_reels",
        params={
            "upload_phase": "start",
            "access_token": META_ACCESS_TOKEN,
        },
        timeout=30,
    )

    if not init_r.ok:
        err  = init_r.json().get("error", {})
        code = err.get("code")
        msg  = err.get("message", "Unknown error")
        if code in (10, 200) or "permission" in msg.lower() or "review" in msg.lower():
            print(f"  SKIPPED: App Review not approved yet.")
            print(f"  Meta error ({code}): {msg}")
            return None
        raise RuntimeError(f"Facebook init failed: {init_r.status_code} — {msg}")

    init_data  = init_r.json()
    video_id   = init_data.get("video_id")
    upload_url = init_data.get("upload_url")

    if not video_id or not upload_url:
        raise RuntimeError(f"Missing video_id or upload_url in response: {init_data}")

    print(f"  Video ID: {video_id}")

    # Step 2: upload video bytes
    print("  Step 2: Uploading video bytes...")
    with open(video_path, "rb") as f:
        upload_r = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {META_ACCESS_TOKEN}",
                "offset":        "0",
                "file_size":     str(file_size),
                "Content-Type":  "application/octet-stream",
            },
            data=f,
            timeout=600,
        )
    upload_r.raise_for_status()
    print("  File uploaded.")

    # Step 3: publish reel
    print("  Step 3: Publishing reel...")
    pub_r = requests.post(
        f"{GRAPH_BASE}/{FACEBOOK_PAGE_ID}/video_reels",
        params={
            "access_token": META_ACCESS_TOKEN,
            "video_id":     video_id,
            "upload_phase": "finish",
            "video_state":  "PUBLISHED",
            "title":        title[:255],
            "description":  description[:2200],
        },
        timeout=30,
    )

    if not pub_r.ok:
        err = pub_r.json().get("error", {})
        raise RuntimeError(f"Facebook publish failed: {err.get('message', pub_r.text[:300])}")

    print(f"  Done! Facebook Reel published. Video ID: {video_id}")
    return video_id


# ══════════════════════════════════════════════════════════════════════════════
# INSTAGRAM REELS
# ══════════════════════════════════════════════════════════════════════════════

def upload_instagram(video_path: str, description: str) -> str:
    print_section("Uploading to Instagram Reels")

    file_size = os.path.getsize(video_path)
    print(f"  File  : {video_path} ({file_size / 1024 / 1024:.1f} MB)")

    # Step 1: create media container (resumable)
    print("  Step 1: Creating media container...")
    init_r = requests.post(
        f"{GRAPH_BASE}/{INSTAGRAM_ACCOUNT_ID}/media",
        params={
            "media_type":   "REELS",
            "upload_type":  "resumable",
            "caption":      description[:2200],
            "access_token": META_ACCESS_TOKEN,
        },
        timeout=30,
    )

    if not init_r.ok:
        err  = init_r.json().get("error", {})
        code = err.get("code")
        msg  = err.get("message", "Unknown error")
        if code in (10, 200) or "permission" in msg.lower() or "review" in msg.lower():
            print(f"  SKIPPED: App Review not approved yet.")
            print(f"  Meta error ({code}): {msg}")
            return None
        raise RuntimeError(f"Instagram init failed: {init_r.status_code} — {msg}")

    init_data    = init_r.json()
    container_id = init_data.get("id")
    upload_url   = init_data.get("uri")

    if not container_id or not upload_url:
        raise RuntimeError(f"Missing id or uri in response: {init_data}")

    print(f"  Container ID: {container_id}")

    # Step 2: upload video bytes
    print("  Step 2: Uploading video bytes...")
    with open(video_path, "rb") as f:
        upload_r = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {META_ACCESS_TOKEN}",
                "offset":        "0",
                "file_size":     str(file_size),
                "Content-Type":  "application/octet-stream",
            },
            data=f,
            timeout=600,
        )
    upload_r.raise_for_status()
    print("  File uploaded. Waiting for Instagram to process...")

    # Step 3: poll until processing is complete
    for attempt in range(24):   # max 4 minutes (24 x 10s)
        time.sleep(10)
        status_r = requests.get(
            f"{GRAPH_BASE}/{container_id}",
            params={
                "fields":       "status_code,status",
                "access_token": META_ACCESS_TOKEN,
            },
            timeout=15,
        )
        status_r.raise_for_status()
        status_data = status_r.json()
        status_code = status_data.get("status_code", "")
        print(f"  Processing status: {status_code} (attempt {attempt + 1}/24)")

        if status_code == "FINISHED":
            break
        if status_code == "ERROR":
            raise RuntimeError(f"Instagram media processing failed: {status_data}")
    else:
        raise RuntimeError("Instagram processing timed out after 4 minutes.")

    # Step 4: publish
    print("  Step 4: Publishing reel...")
    pub_r = requests.post(
        f"{GRAPH_BASE}/{INSTAGRAM_ACCOUNT_ID}/media_publish",
        params={
            "creation_id":  container_id,
            "access_token": META_ACCESS_TOKEN,
        },
        timeout=30,
    )

    if not pub_r.ok:
        err = pub_r.json().get("error", {})
        raise RuntimeError(f"Instagram publish failed: {err.get('message', pub_r.text[:300])}")

    media_id = pub_r.json().get("id", "unknown")
    print(f"  Done! Instagram Reel published. Media ID: {media_id}")
    return media_id


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("Loading video metadata...")
    meta        = load_metadata()
    video_path  = meta.get("video_file", "output_video.mp4")
    title       = meta.get("title", "Quran Recitation")
    description = meta.get("description", "")

    print(f"  Surah : {meta.get('surah_en')} ({meta.get('surah_num')})")
    print(f"  Ayahs : {meta.get('first_ayah')} - {meta.get('last_ayah')}")

    if not Path(video_path).exists():
        print(f"ERROR: Video file not found: {video_path}")
        sys.exit(1)

    results = {}

    # ── YouTube ───────────────────────────────────────────────────────────────
    if YOUTUBE_REFRESH_TOKEN and YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET:
        try:
            results["youtube"] = upload_youtube(video_path, title, description)
        except Exception as e:
            print(f"  ERROR: YouTube upload failed — {e}")
            results["youtube"] = None
    else:
        print("\nYouTube credentials missing — skipping.")
        results["youtube"] = None

    # ── Facebook ──────────────────────────────────────────────────────────────
    if META_ACCESS_TOKEN and FACEBOOK_PAGE_ID:
        try:
            results["facebook"] = upload_facebook(video_path, title, description)
        except Exception as e:
            print(f"  ERROR: Facebook upload failed — {e}")
            results["facebook"] = None
    else:
        print("\nFacebook credentials missing — skipping.")
        results["facebook"] = None

    # ── Instagram ─────────────────────────────────────────────────────────────
    if META_ACCESS_TOKEN and INSTAGRAM_ACCOUNT_ID:
        try:
            results["instagram"] = upload_instagram(video_path, description)
        except Exception as e:
            print(f"  ERROR: Instagram upload failed — {e}")
            results["instagram"] = None
    else:
        print("\nInstagram credentials missing — skipping.")
        results["instagram"] = None

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "═" * 50)
    print("  UPLOAD SUMMARY")
    print("═" * 50)
    print(f"  YouTube   : {'✓ ' + results['youtube'] if results['youtube'] else '✗ failed or skipped'}")
    print(f"  Facebook  : {'✓ ' + results['facebook'] if results['facebook'] else '✗ failed or skipped'}")
    print(f"  Instagram : {'✓ ' + results['instagram'] if results['instagram'] else '✗ failed or skipped'}")

    # Only fail the workflow if YouTube failed — Meta failures are non-fatal
    if results["youtube"] is None and (YOUTUBE_REFRESH_TOKEN and YOUTUBE_CLIENT_ID):
        print("\nFATAL: YouTube upload failed.")
        sys.exit(1)

    print("\nWorkflow complete.")


if __name__ == "__main__":
    main()
