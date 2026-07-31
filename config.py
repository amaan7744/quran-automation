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
#
# NOTE ON THRESHOLDS (audited — see quality_filter.py measure_shake for the
# most important fix):
# - MIN_CLIP_WIDTH/HEIGHT: was 1080x1920 (i.e. required the source file to
#   already be full target resolution). Every downloaded clip is unconditionally
#   rescaled/cropped to VIDEO_WIDTH x VIDEO_HEIGHT in video_effects.py, so this
#   was rejecting perfectly usable 720p-portrait stock footage for no real
#   reason. Relaxed to 720x1280 (a sensible floor for background b-roll that
#   will be upscaled and isn't the focal element on screen).
# - MIN_CLIP_FPS: 24 is correct per the brief and is NOT changed. The bug was
#   in the comparison (see FPS_TOLERANCE below), not the threshold itself.
# - MIN_VIDEO_BITRATE_KBPS: lowered together with the resolution floor — 2500
#   was calibrated for guaranteed-1080p delivery; at the relaxed 720p floor
#   that bitrate is unrealistic for legitimately good footage.
# - MAX_ASPECT_DEVIATION, BLUR_SCORE_MIN: audited, not the cause of the bug,
#   left as-is (blurdetect at 15/100 ~= 0.15 raw is already a lenient cutoff).
MIN_CLIP_WIDTH        = 720
MIN_CLIP_HEIGHT       = 1280
MIN_CLIP_FPS          = 24
FPS_TOLERANCE         = 0.5    # accepts common 23.976 ("24fps") footage; see quality_filter.py
MIN_CLIP_DURATION     = 4
MAX_CLIP_DURATION     = 30
MAX_ASPECT_DEVIATION  = 0.15   # reject if far from a 9:16 portrait ratio
BLUR_SCORE_MIN        = 15.0   # ffmpeg blurdetect: higher blur_avg = blurrier; below this is "sharp enough"
MIN_VIDEO_BITRATE_KBPS = 1500  # was 2500 — recalibrated for the relaxed 720p floor above
# SHAKE_SCORE_MAX: the previous value (12.0) was calibrated against a shake
# metric that was parsing the WRONG file format (see measure_shake) and
# effectively returned meaningless numbers, not real camera-shake magnitude.
# The metric has been rewritten (deshake-vs-original frame-difference); this
# threshold is recalibrated to *that* metric's scale, empirically measured
# against a static clip (~3.5) and a visibly shaky clip (~11.4).
SHAKE_SCORE_MAX        = 9.0

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
