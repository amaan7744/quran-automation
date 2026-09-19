#!/usr/bin/env python3
"""Configuration for the 24/7 Quran YouTube livestream."""

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
LIVE_DIR = ROOT_DIR / "live"
CACHE_DIR = Path(os.environ.get("LIVE_CACHE_DIR", ROOT_DIR / ".cache" / "live_audio"))
STATE_FILE = Path(os.environ.get("LIVE_STATE_FILE", LIVE_DIR / "stream_state.json"))
BACKGROUND_FILE = Path(os.environ.get("LIVE_BACKGROUND", LIVE_DIR / "background.mp4"))
# Live subtitle data/font files.
ARABIC_DATA_FILE = Path(os.environ.get("LIVE_ARABIC_DATA", ROOT_DIR / "arabic.json"))
ENGLISH_DATA_FILE = Path(os.environ.get("LIVE_ENGLISH_DATA", ROOT_DIR / "english.json"))
FONT_DIR = Path(os.environ.get("LIVE_FONT_DIR", ROOT_DIR / "fonts"))
ARABIC_FONT_FILE = Path(os.environ.get(
    "LIVE_ARABIC_FONT",
    FONT_DIR / "NotoNaskhArabic-Regular.ttf",
))
ENGLISH_FONT_FILE = Path(os.environ.get(
    "LIVE_ENGLISH_FONT",
    FONT_DIR / "DejaVuSans.ttf",
))
SUBTITLE_DIR = Path(os.environ.get("LIVE_SUBTITLE_DIR", LIVE_DIR / "subtitles"))
LOG_DIR = Path(os.environ.get("LIVE_LOG_DIR", ROOT_DIR / "logs" / "live"))

# YouTube Live ingest. Keep the stream key in an environment variable/secret.
RTMP_URL = os.environ.get("YOUTUBE_RTMP_URL", "rtmps://a.rtmps.youtube.com/live2")
STREAM_KEY = os.environ.get("YOUTUBE_STREAM_KEY", "")

# 16:9 landscape live stream.
WIDTH = int(os.environ.get("LIVE_WIDTH", "1920"))
HEIGHT = int(os.environ.get("LIVE_HEIGHT", "1080"))
FPS = int(os.environ.get("LIVE_FPS", "30"))

# YouTube-friendly H.264/AAC live settings.
VIDEO_BITRATE = os.environ.get("LIVE_VIDEO_BITRATE", "4500k")
VIDEO_MAXRATE = os.environ.get("LIVE_VIDEO_MAXRATE", VIDEO_BITRATE)
VIDEO_BUFSIZE = os.environ.get("LIVE_VIDEO_BUFSIZE", "9000k")
AUDIO_BITRATE = os.environ.get("LIVE_AUDIO_BITRATE", "160k")
GOP_SECONDS = int(os.environ.get("LIVE_GOP_SECONDS", "2"))

# GitHub Actions mode: run for a bounded number of minutes, then stop cleanly
# after the current ayah so the next scheduled job can resume from stream_state.json.
RUN_MINUTES = int(os.environ.get("LIVE_RUN_MINUTES", "0"))  # 0 = run forever

# EveryAyah source used by the existing project.
EVERYAYAH_BASE = os.environ.get("EVERYAYAH_BASE", "https://everyayah.com/data")
RECITER_FOLDER = os.environ.get(
    "LIVE_RECITER_FOLDER", "Saood_ash-Shuraym_128kbps"
)
RECITER_NAME = os.environ.get("LIVE_RECITER_NAME", "Saud Al-Shuraim")

# Audio is decoded to raw PCM and piped into the one long-running FFmpeg
# process. This avoids building a multi-GB full-Quran master file.
SAMPLE_RATE = int(os.environ.get("LIVE_SAMPLE_RATE", "48000"))
CHANNELS = int(os.environ.get("LIVE_CHANNELS", "2"))
DOWNLOAD_RETRIES = int(os.environ.get("LIVE_DOWNLOAD_RETRIES", "5"))
DOWNLOAD_TIMEOUT = int(os.environ.get("LIVE_DOWNLOAD_TIMEOUT", "45"))

# A small on-disk rolling cache. Files are deleted after successful playback.
# Set >0 to retain completed ayahs for debugging/recovery.
KEEP_PLAYED_AUDIO = os.environ.get("LIVE_KEEP_PLAYED_AUDIO", "false").lower() == "true"

# If the encoder dies, restart it without terminating the Quran sequencing loop.
RESTART_BACKOFF_SECONDS = int(os.environ.get("LIVE_RESTART_BACKOFF_SECONDS", "5"))
MAX_CONSECUTIVE_FFMPEG_FAILURES = int(
    os.environ.get("LIVE_MAX_CONSECUTIVE_FFMPEG_FAILURES", "10")
)

# On a download failure, retry indefinitely with increasing delays rather than
# skipping an ayah. The stream therefore never silently skips Quran audio.
MAX_AYAH_RETRIES = int(os.environ.get("LIVE_MAX_AYAH_RETRIES", "0"))  # 0 = forever
RETRY_BACKOFF_SECONDS = int(os.environ.get("LIVE_RETRY_BACKOFF_SECONDS", "3"))

CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
