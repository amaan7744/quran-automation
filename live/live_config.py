#!/usr/bin/env python3
"""Configuration for the 24/7 Quran YouTube livestream."""

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
LIVE_DIR = ROOT_DIR / "live"
CACHE_DIR = Path(os.environ.get("LIVE_CACHE_DIR", ROOT_DIR / ".cache" / "live_audio"))
STATE_FILE = Path(os.environ.get("LIVE_STATE_FILE", LIVE_DIR / "stream_state.json"))
BACKGROUND_FILE = Path(os.environ.get("LIVE_BACKGROUND", LIVE_DIR / "background.mp4"))
LOG_DIR = Path(os.environ.get("LIVE_LOG_DIR", ROOT_DIR / "logs" / "live"))

# Quran text used by the live Arabic + English subtitle overlay.
ARABIC_DATA_FILE = Path(os.environ.get("LIVE_ARABIC_DATA", ROOT_DIR / "arabic.json"))
ENGLISH_DATA_FILE = Path(os.environ.get("LIVE_ENGLISH_DATA", ROOT_DIR / "english.json"))
FONT_DIR = ROOT_DIR / "fonts"
ARABIC_FONT_FILE = Path(os.environ.get("LIVE_ARABIC_FONT", FONT_DIR / "DigitalKhattIndoPak.otf"))
ENGLISH_FONT_FILE = Path(os.environ.get("LIVE_ENGLISH_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
SUBTITLE_DIR = Path(os.environ.get("LIVE_SUBTITLE_DIR", LIVE_DIR / "subtitles"))

RTMP_URL = os.environ.get("YOUTUBE_RTMP_URL", "rtmps://a.rtmps.youtube.com/live2")
STREAM_KEY = os.environ.get("YOUTUBE_STREAM_KEY", "")

WIDTH = int(os.environ.get("LIVE_WIDTH", "1920"))
HEIGHT = int(os.environ.get("LIVE_HEIGHT", "1080"))
FPS = int(os.environ.get("LIVE_FPS", "30"))
VIDEO_BITRATE = os.environ.get("LIVE_VIDEO_BITRATE", "2500k")
VIDEO_MAXRATE = os.environ.get("LIVE_VIDEO_MAXRATE", VIDEO_BITRATE)
VIDEO_BUFSIZE = os.environ.get("LIVE_VIDEO_BUFSIZE", "5000k")
AUDIO_BITRATE = os.environ.get("LIVE_AUDIO_BITRATE", "128k")
GOP_SECONDS = int(os.environ.get("LIVE_GOP_SECONDS", "2"))
RUN_MINUTES = int(os.environ.get("LIVE_RUN_MINUTES", "0"))

EVERYAYAH_BASE = os.environ.get("EVERYAYAH_BASE", "https://everyayah.com/data")
RECITER_FOLDER = os.environ.get("LIVE_RECITER_FOLDER", "Saood_ash-Shuraym_128kbps")
RECITER_NAME = os.environ.get("LIVE_RECITER_NAME", "Saud Al-Shuraim")

SAMPLE_RATE = int(os.environ.get("LIVE_SAMPLE_RATE", "48000"))
CHANNELS = int(os.environ.get("LIVE_CHANNELS", "2"))
DOWNLOAD_RETRIES = int(os.environ.get("LIVE_DOWNLOAD_RETRIES", "5"))
DOWNLOAD_TIMEOUT = int(os.environ.get("LIVE_DOWNLOAD_TIMEOUT", "45"))
KEEP_PLAYED_AUDIO = os.environ.get("LIVE_KEEP_PLAYED_AUDIO", "false").lower() == "true"
RESTART_BACKOFF_SECONDS = int(os.environ.get("LIVE_RESTART_BACKOFF_SECONDS", "5"))
MAX_CONSECUTIVE_FFMPEG_FAILURES = int(os.environ.get("LIVE_MAX_CONSECUTIVE_FFMPEG_FAILURES", "10"))
MAX_AYAH_RETRIES = int(os.environ.get("LIVE_MAX_AYAH_RETRIES", "0"))
RETRY_BACKOFF_SECONDS = int(os.environ.get("LIVE_RETRY_BACKOFF_SECONDS", "3"))

CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
SUBTITLE_DIR.mkdir(parents=True, exist_ok=True)
