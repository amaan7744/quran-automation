#!/usr/bin/env python3
"""
config.py
Central configuration for the Quran video pipeline.
All tunable constants live here so behavior can be changed
without hunting through module internals.
"""

import os
from pathlib import Path

# ─── OUTPUT VIDEO ────────────────────────────────────────────────────────────
VIDEO_WIDTH   = 1080
VIDEO_HEIGHT  = 1920
VIDEO_FPS     = 60                 # falls back to 30 automatically if source clips are 30fps-only
TARGET_SIZE_MB = 95                # keep under Meta's 100MB Reels ceiling
MIN_VIDEO_BITRATE_KBPS = 2500      # reject candidate clips below this

# ─── DIRECTORIES ─────────────────────────────────────────────────────────────
ROOT_DIR       = Path(__file__).parent
CACHE_DIR      = Path(os.environ.get("CLIP_CACHE_DIR", ROOT_DIR / ".cache" / "clips"))
LOG_DIR        = Path(os.environ.get("LOG_DIR", ROOT_DIR / "logs"))
UPLOAD_HISTORY_FILE = ROOT_DIR / "upload_history.json"
CACHE_INDEX_FILE = CACHE_DIR / "index.json"

CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ─── PEXELS SOURCE ────────────────────────────────────────────────────────────
# NOTE: This project fetches stock nature footage from Pexels, not Pinterest.
# Pinterest has no public API for searching/downloading third-party video
# content, so there is nothing to "improve" there — the Pexels pipeline below
# is the real visual source and has been hardened instead.
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"

NATURE_QUERIES = [
    "waterfall nature", "river flowing forest", "misty forest morning",
    "mountain landscape clouds", "gentle rain window", "clouds timelapse sky",
    "sunset sky clouds", "sunrise golden hour", "flowers blooming macro",
    "leaves wind forest", "night sky stars", "ocean beach waves",
    "snow falling mountain", "desert dunes wind", "moonlight night clouds",
    "lake reflection mountain", "autumn leaves forest", "bamboo forest wind",
    "tropical beach waves", "pine forest fog",
]

# Quality gates — a clip must pass ALL of these to be used
MIN_CLIP_WIDTH        = 1080
MIN_CLIP_HEIGHT       = 1920
MIN_CLIP_FPS          = 24
MIN_CLIP_DURATION     = 4
MAX_CLIP_DURATION     = 30
MAX_ASPECT_DEVIATION  = 0.15   # reject if far from a 9:16 portrait ratio
BLUR_SCORE_MIN        = 15.0   # ffmpeg blurdetect: higher blur_avg = blurrier; below this is "sharp enough"
SHAKE_SCORE_MAX        = 12.0  # vidstabdetect avg shake magnitude ceiling

CLIPS_PER_QUERY   = 6
QUERIES_PER_RUN   = 6
DURATION_BUFFER   = 1.35   # fetch 35% more footage than needed for editing headroom

# ─── VIDEO EDITING (motion / transitions) ────────────────────────────────────
TRANSITION_DURATION = 0.6     # seconds, crossfade/blur transition between clips
MOTION_STYLES = ["zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "drift"]

# ─── SUBTITLES ────────────────────────────────────────────────────────────────
# IndoPak-style Quran font (matches the traditional Mushaf script used across
# India/Pakistan/Bangladesh), based on the Al Majeed 13-line IndoPak Mushaf.
# License: SIL Open Font License 1.1 — free for commercial use/embedding.
# Source: https://github.com/DigitalKhatt/indopakfont (© Amine Anane / Tarteel Inc.)
# See fonts/LICENSE-IndoPak.txt for the full license text and attribution.
ARABIC_FONT       = "DigitalKhatt IndoPak"
ENGLISH_FONT      = "Poppins"  # clean modern sans; installed by the CI workflow
MAX_SUBTITLE_LINES = 2

# ─── AUDIO ────────────────────────────────────────────────────────────────────
AUDIO_TARGET_LUFS   = -16.0     # loudness target for social platforms
AUDIO_TRUE_PEAK     = -1.5
AUDIO_FADE_IN       = 0.4
AUDIO_FADE_OUT      = 0.8

# ─── UPLOADER ─────────────────────────────────────────────────────────────────
GRAPH_VERSION = "v25.0"
MAX_UPLOAD_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 5

HASHTAG_POOL = [
    "#Quran", "#Islam", "#Muslim", "#IslamicReminder", "#QuranRecitation",
    "#Deen", "#Iman", "#Tawheed", "#IslamicQuotes", "#QuranicVerses",
    "#Alhamdulillah", "#SubhanAllah", "#Allah", "#IslamicVideo", "#Reels",
]
