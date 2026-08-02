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
# is the real visual source.
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"

# Cinematic search queries, grouped by visual mood. A reel should feel like
# one intentional environment (a "Forest Reel", a "Mountain Reel", etc.),
# never a random mix of unrelated stock footage — so pexels_fetcher picks
# ONE theme per reel and only searches within that theme's query list
# (see pick_theme() in pexels_fetcher.py). Every query is phrased toward
# cinematic drone/movement footage rather than generic/flat stock shots.
THEMED_QUERIES = {
    "forest": [
        "misty pine forest", "foggy forest morning", "flowing river forest",
        "peaceful forest path", "rain on leaves macro",
        "morning sunlight through trees", "bamboo forest wind",
        "cinematic waterfall forest",
    ],
    "mountain": [
        "cinematic mountain drone", "alpine river drone",
        "clouds above mountains", "sunrise over mountains",
        "aerial valley sunrise", "snow covered mountains",
        "golden hour landscape mountain", "misty mountain peaks",
    ],
    "ocean": [
        "cinematic ocean waves", "aerial coastline drone",
        "sunset over ocean cliffs", "golden hour beach waves",
        "tropical beach aerial drone", "ocean waves cliffs sunset",
    ],
    "winter": [
        "snow falling forest", "frozen lake mist",
        "snow covered mountains drone", "winter fog forest",
        "aurora sky", "moonlit clouds snow",
    ],
    "sky_and_night": [
        "aurora sky timelapse", "stars timelapse night sky",
        "moonlit clouds drifting", "clouds timelapse sky",
        "sunrise golden hour clouds", "night sky stars over mountains",
    ],
}
# Flat fallback list — only used if a themed round comes back completely
# empty across every theme (extremely unlikely; see collect_clips()).
NATURE_QUERIES = [q for group in THEMED_QUERIES.values() for q in group]

# Clip selection — only basic ffprobe metadata is checked, no perceptual
# analysis. A clip is rejected only if: not vertical, resolution below
# MIN_CLIP_WIDTH x MIN_CLIP_HEIGHT, duration below MIN_CLIP_DURATION, or
# corrupted/unreadable by ffprobe.
MIN_CLIP_WIDTH        = 720
MIN_CLIP_HEIGHT       = 1280
MIN_CLIP_DURATION     = 3
MAX_CLIP_DURATION     = 30     # upper bound used only to filter Pexels search results

CLIPS_PER_QUERY   = 6
QUERIES_PER_RUN   = 6
DURATION_BUFFER   = 1.35   # fetch 35% more footage than needed for editing headroom

# Each selected clip is trimmed to a random duration in this range before concatenation.
CLIP_TRIM_MIN = 3.0
CLIP_TRIM_MAX = 5.0

# Subtle, unified cinematic color grade applied identically to every clip
# (see trim_and_normalize() in pexels_fetcher.py) so forest/mountain/ocean
# footage from different Pexels sources doesn't jump between warm/cold or
# flat/saturated looks. Deliberately gentle — this is a light grade, not a
# heavy LUT; the beauty should still come from the footage itself.
GRADE_CONTRAST   = 1.04
GRADE_SATURATION = 0.94
GRADE_BRIGHTNESS = 0.01
# Slight lift of shadows toward cool/teal and highlights toward warm —
# the classic gentle cinematic split-tone, kept subtle enough to be felt
# rather than seen.
GRADE_SHADOW_WARMTH   = -0.02   # negative = slightly cooler shadows
GRADE_HIGHLIGHT_WARMTH = 0.02   # positive = slightly warmer highlights

# ─── VIDEO EDITING (transitions) ─────────────────────────────────────────────
TRANSITION_DURATION = 0.35    # seconds — premium subtle crossfade (~250-400ms target)

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
