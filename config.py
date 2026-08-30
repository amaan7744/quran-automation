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
# True 2K vertical (1440x2560, 9:16) — see the "2K upgrade" pass notes.
# This is NOT 1080x1920 upscaled at the end: every stage of the pipeline
# (background normalization, motion/zoom, color grading, the intro, the
# final composite) renders at this resolution directly — see
# ENCODING QUALITY below and the per-module notes in pexels_fetcher.py,
# video_effects.py, and intro_builder.py for how each stage was updated.
VIDEO_WIDTH   = 1440
VIDEO_HEIGHT  = 2560
VIDEO_FPS     = 60                 # falls back to 30 automatically if source clips are 30fps-only
TARGET_SIZE_MB = 95                # keep under Meta's 100MB Reels ceiling (a platform limit, not resolution-dependent)

# ─── ENCODING QUALITY ─────────────────────────────────────────────────────────
# Two tiers, used consistently across every ffmpeg encode in the repo
# (item 5 of the 2K pass: "review every FFmpeg encode"):
#   FINAL_ENCODE_* — passes that produce pixels that ship in the
#   delivered video: the main composite (background+subtitles+audio),
#   the intro, and the intro/main join. These get the higher-quality
#   settings since there's no further re-encode to "make up for it."
#   INTERMEDIATE_ENCODE_* — per-clip normalization and the background
#   crossfade-concat. These get re-encoded again by the final composite
#   regardless, so a faster preset here has a real speed payoff (item
#   11: GitHub Actions runtime) without being the bottleneck on final
#   quality — crf is still kept fairly tight (19, not 23+) so this
#   intermediate stage doesn't introduce its own visible loss.
# Values are deliberately NOT crf=0-and-preset=veryslow: an absurdly low
# crf just inflates file size for no visible benefit at social-video
# viewing sizes (item 5: "do not use an absurdly low CRF").
FINAL_ENCODE_CRF        = int(os.environ.get("FINAL_ENCODE_CRF", "17"))
FINAL_ENCODE_PRESET     = os.environ.get("FINAL_ENCODE_PRESET", "slow")
INTERMEDIATE_ENCODE_CRF    = int(os.environ.get("INTERMEDIATE_ENCODE_CRF", "19"))
INTERMEDIATE_ENCODE_PRESET = os.environ.get("INTERMEDIATE_ENCODE_PRESET", "faster")

# ─── AUDIO QUALITY ─────────────────────────────────────────────────────────────
# Item 6: 48kHz / 192-256kbps AAC, no aggressive normalization. This is
# the container/codec bitrate for the FINAL muxed audio track; the
# actual loudness target (AUDIO_TARGET_LUFS, further down) is unchanged
# from before this pass — this section only concerns encode quality,
# not making the recitation louder/quieter.
AUDIO_SAMPLE_RATE = 48000
AUDIO_BITRATE     = os.environ.get("AUDIO_BITRATE", "224k")

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

# Cinematic search queries are now grouped into a much larger set of
# visual CATEGORIES (cosmic, night sky, atmospheric light, rain/water,
# desert, Islamic architecture, abstract spiritual, clouds — plus the
# original forest/mountain/ocean/winter/sky_and_night, all still fully
# supported) — see VISUAL_CATEGORIES in visual_themes.py, which is now
# the single source of truth for query pools, category fallback chains,
# and mood weighting. A reel still reads as one intentional environment
# (pexels_fetcher stays on one category per reel — see collect_clips()),
# it just now has far more environments to draw from.

# Clip selection — only basic ffprobe metadata is checked, no perceptual
# analysis. A clip is rejected only if: not vertical, resolution below
# MIN_CLIP_WIDTH x MIN_CLIP_HEIGHT, duration below MIN_CLIP_DURATION, or
# corrupted/unreadable by ffprobe.
#
# Item 3/10 of the 2K pass: "1080p only as a fallback." Raised from the
# old 720x1280 floor (which matched the old 1080x1920 pipeline) to
# 1080x1920 — a source clip that's already below standard-HD portrait
# is genuinely too soft to be a reasonable input for a 2K output and is
# now rejected outright, rather than silently accepted and stretched up
# two resolution tiers. 1080p sources are still explicitly ALLOWED as
# the lowest acceptable fallback tier; search_pexels() separately
# prefers 2160p/1440p over 1080p whenever the Pexels API offers them —
# see SOURCE QUALITY TIERS in pexels_fetcher.py.
MIN_CLIP_WIDTH        = 1080
MIN_CLIP_HEIGHT       = 1920
MIN_CLIP_DURATION     = 3
MAX_CLIP_DURATION     = 30     # upper bound used only to filter Pexels search results

CLIPS_PER_QUERY   = 6
QUERIES_PER_RUN   = 6
DURATION_BUFFER   = 1.35   # fetch 35% more footage than needed for editing headroom

# Each selected clip is trimmed to a random duration in this range before concatenation.
CLIP_TRIM_MIN = 3.0
CLIP_TRIM_MAX = 5.0

# Per-clip color grade is now one of several named cinematic presets
# (deep_night, midnight_blue, warm_gold, moonlight, neutral_cinematic,
# soft_teal, dawn, desert_warmth — item 9) selected per visual template
# — see COLOR_GRADES in visual_themes.py. All are deliberately gentle
# (light grades, not heavy LUTs); the beauty should still come from the
# footage itself.

# ─── ADAPTIVE EXPOSURE (per-clip brightness/highlight correction) ────────────
# The named COLOR_GRADES above set the *style* of a reel (its base
# contrast/saturation/warmth) but apply the same small fixed brightness
# offset to every clip in a category regardless of how bright the
# actual source footage is. A "midnight_blue" grade laid over a sunny
# daytime beach clip still looks like a sunny daytime beach clip with a
# blue tint. This section adds a second, per-clip pass: before grading,
# trim_and_normalize() measures each clip's own average source
# brightness and computes an additional exposure correction layered ON
# TOP of the template's base grade — see pexels_fetcher.
# measure_source_brightness() / compute_adaptive_exposure().
#
# CORE VISUAL RULE: naturally bright footage is never simply rejected —
# it gets intelligently pulled down (lower exposure, tamed highlights,
# slightly reduced saturation) until it fits the dark, muted target
# look. Naturally dark footage is left mostly alone — only a small,
# capped protective lift is applied so shadow detail is never crushed
# into an unreadable black. Mid-range footage is barely touched.
ADAPTIVE_EXPOSURE_ENABLED = os.environ.get("ADAPTIVE_EXPOSURE_ENABLED", "true").lower() not in ("0", "false", "no")
TARGET_AVERAGE_LUMA = 0.36            # desired post-grade average brightness (0=black, 1=white)
DARK_SOURCE_LUMA_FLOOR = 0.16         # below this, treat footage as "already dark" — protect, don't push darker
MAX_BRIGHTNESS_PULLDOWN = 0.30        # hard cap on how far very bright source footage can be pulled down
MAX_SHADOW_PROTECT_LIFT = 0.05        # hard cap on the gentle lift applied to already-dark footage
MAX_HIGHLIGHT_GAMMA_PULLBACK = 0.22   # hard cap on the gamma reduction used to tame blown-out highlights
MAX_BRIGHT_SATURATION_PULLBACK = 0.12 # hard cap on the extra saturation reduction for overly bright footage

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

# ─── CINEMATIC OPENING / BISMILLAH INTRO ──────────────────────────────────────
# See intro_builder.py for the full implementation. This section only
# controls whether/how the intro is built — it never touches subtitles
# or the main recitation audio/video, which are built and validated
# completely independently and then concatenated with the intro as the
# very last step.
INTRO_ENABLED = os.environ.get("INTRO_ENABLED", "true").lower() not in ("0", "false", "no")

# Preferred order (item 18):
#   1. BISMILLAH_AUDIO_PATH, if it points to an existing local file
#      (pre-recorded/licensed audio you supply yourself).
#   2. (same mechanism — just point BISMILLAH_AUDIO_PATH at whichever
#      licensed recording you want to use; there is no separate code
#      path for "licensed" vs "existing local" since both are just a
#      file on disk).
#   3. BISMILLAH_TTS_ENABLED gTTS fallback, if the local file is missing
#      AND this is turned on (off by default — requires network access
#      to Google Translate's TTS endpoint and produces a synthesized,
#      not human-recited, voice; review the output before relying on it
#      for a public channel).
# If neither source is available, the intro is skipped entirely for
# that run (logged clearly) rather than fabricating or downloading
# unlicensed audio — see intro_builder.py.
BISMILLAH_AUDIO_PATH = os.environ.get("BISMILLAH_AUDIO_PATH", str(ROOT_DIR / "assets" / "bismillah.mp3"))
BISMILLAH_TTS_ENABLED = os.environ.get("BISMILLAH_TTS_ENABLED", "false").lower() in ("1", "true", "yes")
BISMILLAH_TTS_CACHE_PATH = ROOT_DIR / ".cache" / "bismillah_tts.mp3"
BISMILLAH_TEXT_ARABIC = "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ"

# Hard bounds on the whole opening sequence (black -> stars/light ->
# Bismillah). Tightened per the "strict intro timing" pass: the target
# is now an extremely short ~1.0-1.5s open, not a 2-4s cinematic intro —
# the viewer should feel an almost immediate transition into the Quran.
# INTRO_TARGET_MAX_DURATION is the soft aim the code tries to land on
# by padding minimally around the actual Bismillah audio (never by
# speeding up or trimming the phrase itself). INTRO_HARD_DURATION_CEILING
# is not a cap that gets enforced by cutting anything — it's the point
# past which a longer-than-normal Bismillah recording gets a loud log
# recommending a shorter clip, while still playing in full.
INTRO_MIN_DURATION = 0.9
INTRO_TARGET_MAX_DURATION = 1.5
INTRO_MAX_DURATION = INTRO_TARGET_MAX_DURATION  # kept as an alias: some callers/docs still refer to this name
INTRO_HARD_DURATION_CEILING = 2.2
# Brief silent black beat before the Bismillah audio starts (item 3:
# "0.0-0.25 sec: black"), so the opening always reads as a deliberate
# beat of stillness rather than audio starting instantly at frame 0.
INTRO_PRE_BLACK = 0.20
INTRO_AUDIO_FADE_OUT = 0.12   # short tail fade so the cut into the main recitation is clean
INTRO_VISUAL_FPS = VIDEO_FPS
# Sanity ceiling, independent of the durations above: if the resolved
# Bismillah audio is longer than this, something is almost certainly
# misconfigured (wrong file pointed to by BISMILLAH_AUDIO_PATH, a full
# reciter track instead of just the Bismillah phrase, etc). Rather than
# building a multi-second-long "intro" that would badly hurt retention,
# the intro is skipped entirely and this is logged loudly — see
# intro_builder.resolve_bismillah_audio(). Tightened from 6.0s to 3.0s
# now that the target intro is ~1.0-1.5s: a "short Bismillah clip" that
# is itself already 3+ seconds long is not a short clip.
INTRO_AUDIO_SANITY_CEILING = 3.0
# Length of the cinematic crossfade joining the intro into the main
# recitation segment (video dissolve + audio crossfade). Shortened
# alongside the overall intro so the Quran recitation is clearly
# underway within ~1.0-1.5s of the video starting, not competing with a
# long dissolve.
INTRO_JOIN_TRANSITION = 0.20

# ─── STAR FIELD (item 1 of the final quality pass) ────────────────────────────
# A genuinely sparse point star field, NOT film-grain/noise presented as
# stars. Generated once per render as a small procedural pattern (see
# intro_builder._build_star_field) and upscaled with a soft blur so each
# point reads as a tiny soft glow rather than a hard pixel or a dense
# "static" texture. Values below are baselines; intro_builder jitters
# them slightly (within *_JITTER) on every render so consecutive videos
# aren't visually identical while keeping the same recognizable
# structure (item 8).
INTRO_STAR_PROBABILITY = 0.0018      # ~35 points on the generation grid — sparse, not a constellation
INTRO_STAR_PROBABILITY_JITTER = 0.0005
INTRO_STAR_OPACITY_RANGE = (0.35, 0.60)   # most of the field stays this subtle even at full fade-in
INTRO_LIGHT_LIFT_RANGE = (0.03, 0.07)     # the very slight overall brightness lift near the end

# ─── VISUAL MOOD ENGINE / EXPERIMENTATION (items 3, 13, 14) ───────────────────
ANALYTICS_FILE = ROOT_DIR / "analytics.json"

# "70-80% proven formats / 20-30% experiments" (item 14), gated by
# statistical confidence (item 5 of the final quality pass — see
# performance_metadata.confidence_tier). These thresholds are counts of
# VIDEOS carrying a given template/bucket that have received a real
# performance score — never raw view/like counts — so one breakout
# video can't single-handedly make a template "proven."
#   < WEAK_SIGNAL_THRESHOLD                    -> exploration only
#   WEAK_SIGNAL_THRESHOLD..MIN_SAMPLES_TO_TRUST -> weak signal (partially trusted)
#   MIN_SAMPLES_TO_TRUST..STRONG_SIGNAL_THRESHOLD -> usable signal (fully trusted)
#   >= STRONG_SIGNAL_THRESHOLD                 -> stronger signal (fully trusted)
EXPERIMENT_EXPLOIT_RATIO = float(os.environ.get("EXPERIMENT_EXPLOIT_RATIO", "0.75"))
WEAK_SIGNAL_THRESHOLD = int(os.environ.get("WEAK_SIGNAL_THRESHOLD", "5"))
MIN_SAMPLES_TO_TRUST = int(os.environ.get("MIN_SAMPLES_TO_TRUST", "10"))
STRONG_SIGNAL_THRESHOLD = int(os.environ.get("STRONG_SIGNAL_THRESHOLD", "20"))
# How much of the base exploit ratio a "weak" signal (5-9 samples) is
# allowed to use — e.g. 0.3 means a weak-signal winner is only exploited
# 0.75*0.3 = 22.5% of the time (vs the full 75% once "usable"/"strong"),
# so it nudges selection without letting a handful of early videos lock
# in a false winner.
WEAK_SIGNAL_EXPLOIT_SCALE = float(os.environ.get("WEAK_SIGNAL_EXPLOIT_SCALE", "0.3"))

# ─── PERFORMANCE SCORE (item 6) ────────────────────────────────────────────────
# Weights for compute_performance_score() in performance_metadata.py —
# kept here, in one visible place, rather than hardcoded inside the
# scoring function, so the calculation stays fully inspectable/tunable.
# Must sum to 1.0 (renormalized automatically over whichever components
# are actually available for a given video — see that function).
PERFORMANCE_SCORE_WEIGHTS = {
    "retention": 0.50,    # average percentage viewed — the primary signal
    "pace": 0.25,         # views/day relative to the channel's recent baseline (age-normalized)
    "subscriber": 0.15,   # subscriber conversion
    "engagement": 0.10,   # likes+comments per view
}

# ─── DURATION EXPERIMENTATION (item 12) ────────────────────────────────────────
# Named duration buckets: (target_max, hard_max) seconds. Ayah integrity
# always wins — build_video.fit_batch_to_duration() never splits an
# ayah regardless of which bucket is chosen, so a bucket is a soft aim,
# not a hard cut point.
DURATION_BUCKETS = {
    "short_18_25":  (25.0, 28.0),
    "medium_25_32": (32.0, 36.0),
    "long_32_40":   (40.0, 45.0),
}

# ─── ANALYTICS INGESTION (item 4) ──────────────────────────────────────────────
# See analytics_ingest.py. This is a SEPARATE, standalone script — not
# run as part of build_video.py or upload.py — meant to be scheduled
# independently (e.g. a second, daily GitHub Actions workflow) since
# YouTube's own numbers (especially Analytics API metrics like average
# view percentage) need time to stabilize after upload.
#
# Requires a YouTube Data API v3 key/OAuth token (view counts/likes/
# comments — the same YOUTUBE_REFRESH_TOKEN used for upload.py already
# covers this) AND, for average-view-duration/percentage/subscribers-
# gained specifically, an OAuth token with the additional
# 'https://www.googleapis.com/auth/yt-analytics.readonly' scope — NOT
# automatically granted by the upload scope alone. If that broader
# scope isn't present, analytics_ingest.py marks those specific fields
# unavailable (never fabricated) and still records whatever it could
# get from the public Data API.
ANALYTICS_MIN_VIDEO_AGE_HOURS = float(os.environ.get("ANALYTICS_MIN_VIDEO_AGE_HOURS", "24"))
YOUTUBE_CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "")
