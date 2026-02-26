#!/usr/bin/env python3
"""
upload.py - Optimized for v25.0
Handles YouTube, Facebook, and Instagram Reels using a single Page Access Token.
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

# Ensure this is a PAGE ACCESS TOKEN with publish_video permission
META_ACCESS_TOKEN     = os.environ.get("META_ACCESS_TOKEN", "")
FACEBOOK_PAGE_ID      = os.environ.get("FACEBOOK_PAGE_ID", "")
INSTAGRAM_ACCOUNT_ID  = os.environ.get("INSTAGRAM_ACCOUNT_ID", "")

METADATA_FILE = Path("video_metadata.json")
GRAPH_VERSION = "v25.0"
GRAPH_BASE    = f"https://graph.facebook.com/{GRAPH_VERSION}"

def load_metadata() -> dict:
    if not METADATA_FILE.exists():
        raise FileNotFoundError("video_metadata.json not found.")
    with open(METADATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def print_section(title: str) -> None:
    print(f"\n{'─' * 50}\n  {title}\n{'─' * 50}")

# ══════════════════════════════════════════════════════════════════════════════
# YOUTUBE (Logic Unchanged)
# ══════════════════════════════════════════════════════════════════════════════

def youtube_get_access_token() -> str:
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": YOUTUBE_CLIENT_ID,
        "client_secret": YOUTUBE_CLIENT_SECRET,
        "refresh_token": YOUTUBE_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    return r.json().get("access_token")

def upload_youtube(video_path: str, title: str, description: str) -> str:
    print_section("Uploading to YouTube Shorts")
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

    with open(video_path, "rb") as f:
        upload_r = requests.put(upload_url, headers={"Content-Length": str(file_size), "Content-Type": "video/mp4"}, data=f, timeout=600)
    upload_r.raise_for_status()
    return upload_r.json().get("id", "unknown")

# ══════════════════════════════════════════════════════════════════════════════
# FACEBOOK REELS (v25.0 Optimized)
# ══════════════════════════════════════════════════════════════════════════════

def upload_facebook(video_path: str, title: str, description: str) -> str:
    print_section("Uploading to Facebook Reels")
    file_size = os.path.getsize(video_path)

    # Step 1: Initialize (graph.facebook.com)
    init_r = requests.post(
        f"{GRAPH_BASE}/{FACEBOOK_PAGE_ID}/video_reels",
        json={"upload_phase": "start", "access_token": META_ACCESS_TOKEN},
        timeout=30)
    
    init_data = init_r.json()
    video_id = init_data.get("video_id")
    upload_url = init_data.get("upload_url") # Should be rupload.facebook.com

    if not video_id:
        print(f"  FAILED Step 1: {init_data}")
        return None

    # Step 2: Upload Bytes (rupload.facebook.com)
    # Using 'Authorization: OAuth' header as required by documentation
    print(f"  Step 2: Uploading {file_size} bytes to Meta Ruploader...")
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

    # Step 3: Publish (graph.facebook.com)
    print("  Step 3: Finishing and Publishing...")
    pub_r = requests.post(
        f"{GRAPH_BASE}/{FACEBOOK_PAGE_ID}/video_reels",
        params={
            "access_token": META_ACCESS_TOKEN,
            "video_id": video_id,
            "upload_phase": "finish",
            "video_state": "PUBLISHED",
            "description": description[:2200],
        }, timeout=30)
    
    if pub_r.ok:
        print(f"  SUCCESS: Facebook Reel Published (ID: {video_id})")
        return video_id
    print(f"  FAILED Step 3: {pub_r.text}")
    return None

# ══════════════════════════════════════════════════════════════════════════════
# INSTAGRAM REELS (v25.0 Optimized)
# ══════════════════════════════════════════════════════════════════════════════

def upload_instagram(video_path: str, description: str) -> str:
    print_section("Uploading to Instagram Reels")
    file_size = os.path.getsize(video_path)

    # Step 1: Create Container
    init_r = requests.post(
        f"{GRAPH_BASE}/{INSTAGRAM_ACCOUNT_ID}/media",
        params={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": description[:2200],
            "access_token": META_ACCESS_TOKEN,
        }, timeout=30)
    
    init_data = init_r.json()
    container_id = init_data.get("id")
    upload_url = init_data.get("uri") # Instagram's rupload URI

    if not container_id:
        print(f"  FAILED Step 1: {init_data}")
        return None

    # Step 2: Upload Bytes
    print("  Step 2: Uploading bytes...")
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

    # Step 3: Poll Status
    print("  Step 3: Waiting for processing...")
    for _ in range(12): # 2 minutes max
        time.sleep(10)
        status_r = requests.get(f"{GRAPH_BASE}/{container_id}", params={
            "fields": "status_code", "access_token": META_ACCESS_TOKEN
        }).json()
        if status_res := status_r.get("status_code") == "FINISHED":
            break
        print(f"    Current status: {status_r.get('status_code')}")

    # Step 4: Publish
    print("  Step 4: Publishing...")
    pub_r = requests.post(f"{GRAPH_BASE}/{INSTAGRAM_ACCOUNT_ID}/media_publish", params={
        "creation_id": container_id, "access_token": META_ACCESS_TOKEN
    }, timeout=30)
    
    if pub_r.ok:
        res_id = pub_r.json().get("id")
        print(f"  SUCCESS: Instagram Reel Published (ID: {res_id})")
        return res_id
    return None

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    try:
        meta = load_metadata()
        video_path = meta.get("video_file", "output_video.mp4")
        title = meta.get("title", "Quran Recitation")
        description = meta.get("description", "")
        
        results = {"youtube": None, "facebook": None, "instagram": None}

        if YOUTUBE_REFRESH_TOKEN:
            results["youtube"] = upload_youtube(video_path, title, description)
        
        if META_ACCESS_TOKEN:
            results["facebook"] = upload_facebook(video_path, title, description)
            results["instagram"] = upload_instagram(video_path, description)

        print("\n" + "═"*50 + "\n  FINAL SUMMARY\n" + "═"*50)
        print(f"  YouTube:   {results['youtube'] or 'FAILED'}")
        print(f"  Facebook:  {results['facebook'] or 'FAILED'}")
        print(f"  Instagram: {results['instagram'] or 'FAILED'}")

    except Exception as e:
        print(f"FATAL ERROR: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
