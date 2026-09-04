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
# NOTE: this file lives at longform/longform_config.py, one level below the
# repo root — unlike the root config.py (where Path(__file__).parent IS the
# repo root). .parent.parent here is what makes ROOT_DIR the actual repo
# root, so QURAN_ARABIC_JSON/QURAN_ENGLISH_JSON below correctly resolve to
# the real arabic.json/english.json at the repo root, and LONGFORM_YAML_FILE
# further down correctly resolves to longform/longform.yml instead of a
# nonexistent longform/longform/longform.yml.
ROOT_DIR       = Path(__file__).resolve().parent.parent
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

# ═══════════════════════════════════════════════════════════════════════════
# LONG-FORM (FULL-SURAH) PIPELINE
# ═══════════════════════════════════════════════════════════════════════════
# Additive block for the full-Surah YouTube long-form pipeline (see
# surah_builder.py / build_surah.py). Nothing above this line is read with
# a different value by the long-form code, and nothing in the short-form
# Reels pipeline (build_video.py / upload.py) reads anything below this
# line — the two pipelines share code (audio mastering, human/quality
# filtering, OAuth/upload primitives) but not tunables, so changing a
# LONGFORM_* value here can never change Shorts/Reels output.

# ─── OUTPUT VIDEO ────────────────────────────────────────────────────────────
LONGFORM_VIDEO_WIDTH  = 3840
LONGFORM_VIDEO_HEIGHT = 2160
LONGFORM_VIDEO_FPS    = 30
LONGFORM_VIDEO_CRF    = 18          # libx264 "high-quality 4K" range is 16-20
LONGFORM_AUDIO_BITRATE = "320k"
LONGFORM_AUDIO_SAMPLE_RATE = 48000

# ─── DIRECTORIES ─────────────────────────────────────────────────────────────
LONGFORM_CACHE_DIR = Path(os.environ.get("LONGFORM_CLIP_CACHE_DIR", ROOT_DIR / ".cache" / "longform_clips"))
LONGFORM_CACHE_INDEX_FILE = LONGFORM_CACHE_DIR / "index.json"
LONGFORM_META_CACHE_FILE  = LONGFORM_CACHE_DIR / "surah_meta_cache.json"
LONGFORM_OUTPUT_DIR = Path(os.environ.get("LONGFORM_OUTPUT_DIR", ROOT_DIR / "output" / "surahs"))
LONGFORM_WORK_DIR   = Path(os.environ.get("LONGFORM_WORK_DIR", ROOT_DIR / ".longform_work"))
LONGFORM_UPLOAD_HISTORY_FILE = ROOT_DIR / "longform_video_metadata.json"
# Anchored to ROOT_DIR (not a bare relative Path) so every long-form run
# reads the same arabic.json/english.json regardless of the working
# directory the CLI happens to be invoked from.
QURAN_ARABIC_JSON  = ROOT_DIR / "arabic.json"
QURAN_ENGLISH_JSON = ROOT_DIR / "english.json"
LONGFORM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
LONGFORM_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LONGFORM_WORK_DIR.mkdir(parents=True, exist_ok=True)

# ─── BACKGROUND FOOTAGE (landscape) ──────────────────────────────────────────
# Same Pexels source, same THEMED_QUERIES moods, same human/vehicle content
# filter as Shorts — only orientation/resolution/duration differ, so these
# are threaded as parameters into the existing pexels_fetcher/quality_filter
# functions rather than duplicated (see their `orientation=` params).
MIN_LONGFORM_CLIP_WIDTH  = 2560   # 1440p floor per spec section 10 (4K preferred, 1440p acceptable)
MIN_LONGFORM_CLIP_HEIGHT = 1440
# Anything at/above this is logged as true native 4K source footage;
# anything accepted between MIN_LONGFORM_CLIP_* and this is logged as an
# upscale-on-render fallback so a 1440p-sourced background is never
# silently presented as if it were native 4K (spec: "never silently
# accept poor-quality footage").
NATIVE_4K_CLIP_WIDTH  = 3840
NATIVE_4K_CLIP_HEIGHT = 2160
# A long-form scene should breathe far longer than a Reel cut — 10-20s per
# clip, never "every few seconds" (spec section 11/12).
LONGFORM_CLIP_TRIM_MIN = 12.0
LONGFORM_CLIP_TRIM_MAX = 20.0
# Source clips must run at least this long to trim a full LONGFORM_CLIP_TRIM_MAX
# segment out of them with room to spare — NOT the same thing as the Shorts
# MIN_CLIP_DURATION (3s), which only needs to cover a ~5s trim.
LONGFORM_SOURCE_CLIP_MIN_DURATION = LONGFORM_CLIP_TRIM_MAX + 3.0
LONGFORM_SOURCE_CLIP_MAX_DURATION = 90.0  # Shorts' 30s cap is unnecessarily tight for 12-20s trims
LONGFORM_TRANSITION_DURATION = 1.5   # slow crossfade, not the Reels 0.35s snap
LONGFORM_DURATION_BUFFER = 1.15
LONGFORM_QUERIES_PER_RUN = 6
LONGFORM_CLIPS_PER_QUERY = 8
LONGFORM_MAX_GATHER_ROUNDS = 8
# xfade filter graphs get unstable/slow well before 100s of simultaneous
# ffmpeg inputs. Background assembly batches clips into segments of this
# size, each rendered to an intermediate file, then stitched with a
# zero-recompression concat demuxer pass (see surah_backgrounds.py).
LONGFORM_SEGMENT_CLIP_COUNT = 24

# ─── SUBTITLES (landscape safe-area layout) ──────────────────────────────────
LONGFORM_ARABIC_FONT  = ARABIC_FONT
LONGFORM_ENGLISH_FONT = ENGLISH_FONT
LONGFORM_TRANSLATION_SOURCE = os.environ.get("LONGFORM_TRANSLATION_SOURCE", "Saheeh International")

# ─── AUDIO / RECITER ──────────────────────────────────────────────────────────
LONGFORM_RECITER_NAME = os.environ.get("LONGFORM_RECITER_NAME", "Saud Al-Shuraim")

# ─── INTRO / OUTRO / THUMBNAIL TOGGLES ────────────────────────────────────────
LONGFORM_INTRO_ENABLED = os.environ.get("LONGFORM_INTRO", "true").lower() == "true"
LONGFORM_OUTRO_ENABLED = os.environ.get("LONGFORM_OUTRO", "true").lower() == "true"
LONGFORM_OUTRO_MESSAGE = os.environ.get("LONGFORM_OUTRO_MESSAGE", "May Allah accept our worship")
LONGFORM_THUMBNAIL_ENABLED = os.environ.get("LONGFORM_THUMBNAIL", "true").lower() == "true"
LONGFORM_INTRO_DURATION = 6.0
LONGFORM_OUTRO_DURATION = 8.0

# ─── YOUTUBE UPLOAD ────────────────────────────────────────────────────────────
LONGFORM_UPLOAD_ENABLED  = os.environ.get("LONGFORM_UPLOAD", "true").lower() == "true"
LONGFORM_UPLOAD_PRIVACY  = os.environ.get("LONGFORM_UPLOAD_PRIVACY", "private")   # private|unlisted|public
LONGFORM_UPLOAD_CATEGORY = os.environ.get("LONGFORM_UPLOAD_CATEGORY", "22")       # "People & Blogs"
LONGFORM_PLAYLIST_ID     = os.environ.get("LONGFORM_PLAYLIST_ID", "")
# Whether to configure YouTube's native scheduled-publish (status.publishAt)
# after a successful private upload, targeting the schedule: block below.
LONGFORM_SCHEDULE_PUBLISH = os.environ.get("LONGFORM_SCHEDULE_PUBLISH", "true").lower() == "true"
# Practical cap on individual YouTube chapter timestamps in the description
# before section 19's "intelligently group chapters" rule kicks in.
LONGFORM_CHAPTER_MAX_COUNT = 90

# ─── WEEKLY SCHEDULE / SEQUENTIAL SURAH STATE ────────────────────────────────
# Defaults here are only a fallback of last resort — the real source of
# truth is longform.yml, loaded below. Kept in sync with longform.yml's
# `schedule:` / `surah:` blocks so the module still works if the YAML file
# is ever missing.
LONGFORM_ENABLED = True
LONGFORM_SCHEDULE_DAY = "Friday"
LONGFORM_SCHEDULE_TIME = "19:00"
LONGFORM_SCHEDULE_TIMEZONE = "Asia/Kolkata"
LONGFORM_SCHEDULE_GRACE_MINUTES = 90
LONGFORM_SURAH_START = 1
LONGFORM_SURAH_WRAP = True
LONGFORM_SCHEDULE_STATE_FILE = ROOT_DIR / "longform_schedule_state.json"

# ─── longform/longform.yml LOADER ─────────────────────────────────────────────
# Long-form-specific values live in longform/longform.yml (spec: "do NOT
# scatter long-form values throughout the Python code"). This loads it once,
# at import time, and overrides ONLY the LONGFORM_* names above/in this
# section — nothing above the "LONGFORM (FULL-SURAH) PIPELINE" banner is
# touched, so Shorts/Reels behavior can never change because of this file.
# Missing file, missing keys, or a missing/broken PyYAML install all
# degrade to "keep the Python fallback" rather than crashing either
# pipeline.
# NOTE: this app-config YAML lives under longform/ — it is NOT the GitHub
# Actions workflow (.github/workflows/longform-weekly.yml). Never point
# this at anything under .github/workflows/: a workflow file is CI wiring,
# this file is pipeline configuration, and GitHub will fail to parse the
# workflows directory if the two are ever mixed up.
LONGFORM_YAML_FILE = ROOT_DIR / "longform" / "longform.yml"


def _load_longform_yaml() -> dict:
    if not LONGFORM_YAML_FILE.exists():
        return {}
    try:
        import yaml
    except ImportError:
        print(f"[config] PyYAML not installed — ignoring {LONGFORM_YAML_FILE.name}, "
              f"using built-in long-form defaults. Run: pip install pyyaml")
        return {}
    try:
        return yaml.safe_load(LONGFORM_YAML_FILE.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"[config] Could not parse {LONGFORM_YAML_FILE.name} ({e}) — "
              f"using built-in long-form defaults.")
        return {}


_lf = _load_longform_yaml()
if _lf:
    LONGFORM_ENABLED = bool(_lf.get("enabled", LONGFORM_ENABLED))

    _sched = _lf.get("schedule") or {}
    LONGFORM_SCHEDULE_DAY = _sched.get("day", LONGFORM_SCHEDULE_DAY)
    LONGFORM_SCHEDULE_TIME = _sched.get("time", LONGFORM_SCHEDULE_TIME)
    LONGFORM_SCHEDULE_TIMEZONE = _sched.get("timezone", LONGFORM_SCHEDULE_TIMEZONE)
    LONGFORM_SCHEDULE_GRACE_MINUTES = int(_sched.get("grace_minutes", LONGFORM_SCHEDULE_GRACE_MINUTES))

    _vid = _lf.get("video") or {}
    LONGFORM_VIDEO_WIDTH = int(_vid.get("width", LONGFORM_VIDEO_WIDTH))
    LONGFORM_VIDEO_HEIGHT = int(_vid.get("height", LONGFORM_VIDEO_HEIGHT))
    LONGFORM_VIDEO_FPS = int(_vid.get("fps", LONGFORM_VIDEO_FPS))
    LONGFORM_VIDEO_CRF = int(_vid.get("crf", LONGFORM_VIDEO_CRF))
    LONGFORM_AUDIO_BITRATE = str(_vid.get("audio_bitrate", LONGFORM_AUDIO_BITRATE))
    LONGFORM_AUDIO_SAMPLE_RATE = int(_vid.get("audio_sample_rate", LONGFORM_AUDIO_SAMPLE_RATE))

    _bg = _lf.get("background") or {}
    MIN_LONGFORM_CLIP_WIDTH = int(_bg.get("min_width", MIN_LONGFORM_CLIP_WIDTH))
    MIN_LONGFORM_CLIP_HEIGHT = int(_bg.get("min_height", MIN_LONGFORM_CLIP_HEIGHT))
    NATIVE_4K_CLIP_WIDTH = int(_bg.get("native_4k_width", NATIVE_4K_CLIP_WIDTH))
    NATIVE_4K_CLIP_HEIGHT = int(_bg.get("native_4k_height", NATIVE_4K_CLIP_HEIGHT))
    LONGFORM_CLIP_TRIM_MIN = float(_bg.get("clip_trim_min", LONGFORM_CLIP_TRIM_MIN))
    LONGFORM_CLIP_TRIM_MAX = float(_bg.get("clip_trim_max", LONGFORM_CLIP_TRIM_MAX))
    LONGFORM_TRANSITION_DURATION = float(_bg.get("transition_duration", LONGFORM_TRANSITION_DURATION))
    LONGFORM_SOURCE_CLIP_MIN_DURATION = LONGFORM_CLIP_TRIM_MAX + 3.0

    _sub = _lf.get("subtitles") or {}
    LONGFORM_ARABIC_FONT = _sub.get("arabic_font", LONGFORM_ARABIC_FONT)
    LONGFORM_ENGLISH_FONT = _sub.get("english_font", LONGFORM_ENGLISH_FONT)
    LONGFORM_TRANSLATION_SOURCE = _sub.get("translation_source", LONGFORM_TRANSLATION_SOURCE)

    _io = _lf.get("intro_outro") or {}
    LONGFORM_INTRO_ENABLED = bool(_io.get("intro_enabled", LONGFORM_INTRO_ENABLED))
    LONGFORM_OUTRO_ENABLED = bool(_io.get("outro_enabled", LONGFORM_OUTRO_ENABLED))
    LONGFORM_INTRO_DURATION = float(_io.get("intro_duration", LONGFORM_INTRO_DURATION))
    LONGFORM_OUTRO_DURATION = float(_io.get("outro_duration", LONGFORM_OUTRO_DURATION))
    LONGFORM_OUTRO_MESSAGE = _io.get("outro_message", LONGFORM_OUTRO_MESSAGE)

    _thumb = _lf.get("thumbnail") or {}
    LONGFORM_THUMBNAIL_ENABLED = bool(_thumb.get("enabled", LONGFORM_THUMBNAIL_ENABLED))

    _yt = _lf.get("youtube") or {}
    # "upload_privacy" is the current key; "privacy" (pre-scheduling schema)
    # is still accepted as a fallback so an un-migrated longform.yml doesn't
    # silently lose its configured privacy status.
    LONGFORM_UPLOAD_PRIVACY = _yt.get("upload_privacy", _yt.get("privacy", LONGFORM_UPLOAD_PRIVACY))
    LONGFORM_UPLOAD_CATEGORY = str(_yt.get("category", LONGFORM_UPLOAD_CATEGORY))
    LONGFORM_PLAYLIST_ID = _yt.get("playlist_id", LONGFORM_PLAYLIST_ID)
    LONGFORM_SCHEDULE_PUBLISH = bool(_yt.get("schedule_publish", LONGFORM_SCHEDULE_PUBLISH))

    _surah_cfg = _lf.get("surah") or {}
    LONGFORM_SURAH_START = int(_surah_cfg.get("start", LONGFORM_SURAH_START))
    LONGFORM_SURAH_WRAP = bool(_surah_cfg.get("wrap", LONGFORM_SURAH_WRAP))

del _lf

# Explicit env vars still win over longform.yml for one-off runs (e.g. a CI
# job overriding privacy for a single manual dispatch) — yml only sets the
# checked-in baseline.
if os.environ.get("LONGFORM_UPLOAD") is not None:
    LONGFORM_UPLOAD_ENABLED = os.environ["LONGFORM_UPLOAD"].lower() == "true"
if os.environ.get("LONGFORM_UPLOAD_PRIVACY") is not None:
    LONGFORM_UPLOAD_PRIVACY = os.environ["LONGFORM_UPLOAD_PRIVACY"]
if os.environ.get("LONGFORM_UPLOAD_CATEGORY") is not None:
    LONGFORM_UPLOAD_CATEGORY = os.environ["LONGFORM_UPLOAD_CATEGORY"]
if os.environ.get("LONGFORM_PLAYLIST_ID") is not None:
    LONGFORM_PLAYLIST_ID = os.environ["LONGFORM_PLAYLIST_ID"]
if os.environ.get("LONGFORM_SCHEDULE_PUBLISH") is not None:
    LONGFORM_SCHEDULE_PUBLISH = os.environ["LONGFORM_SCHEDULE_PUBLISH"].lower() == "true"
if os.environ.get("LONGFORM_INTRO") is not None:
    LONGFORM_INTRO_ENABLED = os.environ["LONGFORM_INTRO"].lower() == "true"
if os.environ.get("LONGFORM_OUTRO") is not None:
    LONGFORM_OUTRO_ENABLED = os.environ["LONGFORM_OUTRO"].lower() == "true"
if os.environ.get("LONGFORM_OUTRO_MESSAGE") is not None:
    LONGFORM_OUTRO_MESSAGE = os.environ["LONGFORM_OUTRO_MESSAGE"]
if os.environ.get("LONGFORM_THUMBNAIL") is not None:
    LONGFORM_THUMBNAIL_ENABLED = os.environ["LONGFORM_THUMBNAIL"].lower() == "true"
if os.environ.get("LONGFORM_RECITER_NAME") is not None:
    LONGFORM_RECITER_NAME = os.environ["LONGFORM_RECITER_NAME"]
if os.environ.get("LONGFORM_TRANSLATION_SOURCE") is not None:
    LONGFORM_TRANSLATION_SOURCE = os.environ["LONGFORM_TRANSLATION_SOURCE"]
