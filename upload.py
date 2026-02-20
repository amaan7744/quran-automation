#!/usr/bin/env python3
"""
Upload the built video to:
  - YouTube Shorts (via YouTube Data API v3)
  - Facebook Reels (via Meta Graph API)
  - Instagram Reels (via Meta Graph API)

Reads video_metadata.json written by build_video.py.
All credentials come from environment variables (GitHub Secrets).
"""

import os
import sys
import json
import time
import requests
from pathlib import Path

# ─── ENV VARS (GitHub Secrets) ────────────────────────────────────────────────
YOUTUBE_CLIENT_ID       = os.environ.get("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET   = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN   = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")

META_ACCESS_TOKEN       = os.environ.get("META_ACCESS_TOKEN", "")
FACEBOOK_PAGE_ID        = os.environ.get("FACEBOOK_PAGE_ID", "")
INSTAGRAM_ACCOUNT_ID    = os.environ.get("INSTAGRAM_ACCOUNT_ID", "")
# ──────────────────────────────────────────────────────────────────────────────

METADATA_FILE = Path("video_metadata.json")


def load_metadata():
    if not METADATA_FILE.exists():
        raise FileNotFoundError("video_metadata.json not found. Run build_video.py first.")
    with open(METADATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# ─── YOUTUBE ──────────────────────────────────────────────────────────────────
def youtube_get_access_token():
    """Exchange refresh token for a short-lived access token."""
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id":     YOUTUBE_CLIENT_ID,
        "client_secret": YOUTUBE_CLIENT_SECRET,
        "refresh_token": YOUTUBE_REFRESH_TOKEN,
        "grant_type":    "refresh_token",
    })
    r.raise_for_status()
    return r.json()["access_token"]


def upload_youtube(video_path, title, description):
    print("\n── Uploading to YouTube Shorts ──")
    access_token = youtube_get_access_token()

    # Step 1: initiate resumable upload
    metadata = {
        "snippet": {
            "title":       title[:100],   # YouTube title limit
            "description": description,
            "tags":        ["Quran", "Islam", "Recitation", "Shorts"],
            "categoryId":  "22",          # People & Blogs
        },
        "status": {
            "privacyStatus":          "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    init_r = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos"
        "?uploadType=resumable&part=snippet,status",
        headers={
            "Authorization":  f"Bearer {access_token}",
            "Content-Type":   "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
        },
        json=metadata,
    )
    init_r.raise_for_status()
    upload_url = init_r.headers["Location"]

    # Step 2: upload video bytes
    file_size = os.path.getsize(video_path)
    with open(video_path, "rb") as f:
        upload_r = requests.put(
            upload_url,
            headers={
                "Content-Length": str(file_size),
                "Content-Type":   "video/mp4",
            },
            data=f,
            timeout=300,
        )
    upload_r.raise_for_status()
    video_id = upload_r.json().get("id", "unknown")
    print(f"  YouTube upload done. Video ID: {video_id}")
    return video_id


# ─── FACEBOOK ─────────────────────────────────────────────────────────────────
def upload_facebook(video_path, title, description):
    """
    Upload as Facebook Reel using the Resumable Upload flow.
    Docs: https://developers.facebook.com/docs/video-api/guides/reels-publishing
    """
    print("\n── Uploading to Facebook Reels ──")
    file_size = os.path.getsize(video_path)

    # Step 1: Initialize upload session
    init_r = requests.post(
        f"https://graph.facebook.com/v19.0/{FACEBOOK_PAGE_ID}/video_reels",
        params={
            "upload_phase": "start",
            "access_token": META_ACCESS_TOKEN,
        }
    )
    init_r.raise_for_status()
    video_id    = init_r.json()["video_id"]
    upload_url  = init_r.json()["upload_url"]
    print(f"  FB upload session created. Video ID: {video_id}")

    # Step 2: Upload the file
    with open(video_path, "rb") as f:
        upload_r = requests.post(
            upload_url,
            headers={
                "Authorization":        f"OAuth {META_ACCESS_TOKEN}",
                "offset":               "0",
                "file_size":            str(file_size),
                "Content-Type":         "application/octet-stream",
            },
            data=f,
            timeout=300,
        )
    upload_r.raise_for_status()
    print("  FB file upload done.")

    # Step 3: Publish
    pub_r = requests.post(
        f"https://graph.facebook.com/v19.0/{FACEBOOK_PAGE_ID}/video_reels",
        params={
            "access_token":  META_ACCESS_TOKEN,
            "video_id":      video_id,
            "upload_phase":  "finish",
            "video_state":   "PUBLISHED",
            "description":   description[:2200],
            "title":         title[:255],
        }
    )
    pub_r.raise_for_status()
    print(f"  Facebook Reel published. Video ID: {video_id}")
    return video_id


# ─── INSTAGRAM ────────────────────────────────────────────────────────────────
def upload_instagram(video_path, title, description):
    """
    Upload as Instagram Reel.
    Instagram requires the video to be publicly accessible via URL.
    We use the Graph API resumable upload to get a hosted video URL first,
    then create the media container and publish.
    """
    print("\n── Uploading to Instagram Reels ──")

    # Step 1: Create upload session on Instagram
    session_r = requests.post(
        f"https://graph.facebook.com/v19.0/{INSTAGRAM_ACCOUNT_ID}/media",
        params={
            "media_type":   "REELS",
            "caption":      description[:2200],
            "access_token": META_ACCESS_TOKEN,
        }
    )
    # Instagram Reels need a video_url (publicly hosted).
    # For the GitHub Actions flow we upload to Facebook first then link,
    # OR use the upload_url approach introduced in Graph API v17+.

    # Use the /media endpoint with upload_type=resumable
    init_r = requests.post(
        f"https://graph.facebook.com/v19.0/{INSTAGRAM_ACCOUNT_ID}/media",
        params={
            "media_type":   "REELS",
            "upload_type":  "resumable",
            "caption":      description[:2200],
            "access_token": META_ACCESS_TOKEN,
        }
    )
    init_r.raise_for_status()
    ig_container_id = init_r.json().get("id")
    upload_url      = init_r.json().get("uri")  # resumable upload URI

    if not upload_url:
        raise RuntimeError(f"Instagram did not return upload URI. Response: {init_r.json()}")

    print(f"  IG container ID: {ig_container_id}")

    # Step 2: Upload video bytes
    file_size = os.path.getsize(video_path)
    with open(video_path, "rb") as f:
        up_r = requests.post(
            upload_url,
            headers={
                "Authorization":  f"OAuth {META_ACCESS_TOKEN}",
                "offset":         "0",
                "file_size":      str(file_size),
                "Content-Type":   "application/octet-stream",
            },
            data=f,
            timeout=300,
        )
    up_r.raise_for_status()
    print("  IG file upload done. Waiting for processing...")

    # Step 3: Poll until STATUS = FINISHED
    for _ in range(20):
        time.sleep(10)
        status_r = requests.get(
            f"https://graph.facebook.com/v19.0/{ig_container_id}",
            params={
                "fields":       "status_code",
                "access_token": META_ACCESS_TOKEN,
            }
        )
        status_r.raise_for_status()
        status_code = status_r.json().get("status_code")
        print(f"  IG status: {status_code}")
        if status_code == "FINISHED":
            break
        if status_code == "ERROR":
            raise RuntimeError("Instagram media processing failed.")

    # Step 4: Publish
    pub_r = requests.post(
        f"https://graph.facebook.com/v19.0/{INSTAGRAM_ACCOUNT_ID}/media_publish",
        params={
            "creation_id":  ig_container_id,
            "access_token": META_ACCESS_TOKEN,
        }
    )
    pub_r.raise_for_status()
    ig_media_id = pub_r.json().get("id")
    print(f"  Instagram Reel published. Media ID: {ig_media_id}")
    return ig_media_id


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    meta = load_metadata()
    video_path  = meta["video_file"]
    title       = meta["title"]
    description = meta["description"]

    if not Path(video_path).exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    errors = []

    if YOUTUBE_REFRESH_TOKEN:
        try:
            upload_youtube(video_path, title, description)
        except Exception as e:
            print(f"  YouTube upload FAILED: {e}")
            errors.append(f"YouTube: {e}")
    else:
        print("YouTube credentials not set, skipping.")

    if META_ACCESS_TOKEN and FACEBOOK_PAGE_ID:
        try:
            upload_facebook(video_path, title, description)
        except Exception as e:
            print(f"  Facebook upload FAILED: {e}")
            errors.append(f"Facebook: {e}")
    else:
        print("Facebook credentials not set, skipping.")

    if META_ACCESS_TOKEN and INSTAGRAM_ACCOUNT_ID:
        try:
            upload_instagram(video_path, title, description)
        except Exception as e:
            print(f"  Instagram upload FAILED: {e}")
            errors.append(f"Instagram: {e}")
    else:
        print("Instagram credentials not set, skipping.")

    if errors:
        print("\nSome uploads failed:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("\nAll uploads completed successfully.")


if __name__ == "__main__":
    main()
