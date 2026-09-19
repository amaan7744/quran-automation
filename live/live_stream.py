#!/usr/bin/env python3
"""
24/7 Quran livestream.

Architecture:
    EveryAyah MP3 -> per-ayah decode -> raw PCM stdin
                                      |
    looping MP4 background -> FFmpeg --+--> H.264/AAC -> YouTube RTMPS

There is deliberately ONE long-running FFmpeg process while the Python
supervisor feeds it Quran audio in strict mushaf order. The background never
changes. Only the audio advances.

The stream does not use progress.json/QURAN_PROGRESS because those belong to
the Shorts pipeline. Livestream state is stored independently in live/.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

# Make root modules importable when launched as `python live/live_stream.py`.
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import requests

from surah_data import SURAHS
from live.live_subtitles import prepare_subtitles, subtitle_files
from live.live_config import (
    AUDIO_BITRATE,
    ARABIC_FONT_FILE,
    ENGLISH_FONT_FILE,
    SUBTITLE_DIR,
    BACKGROUND_FILE,
    CACHE_DIR,
    CHANNELS,
    DOWNLOAD_RETRIES,
    DOWNLOAD_TIMEOUT,
    EVERYAYAH_BASE,
    FPS,
    GOP_SECONDS,
    HEIGHT,
    KEEP_PLAYED_AUDIO,
    MAX_AYAH_RETRIES,
    MAX_CONSECUTIVE_FFMPEG_FAILURES,
    RECITER_FOLDER,
    RECITER_NAME,
    RESTART_BACKOFF_SECONDS,
    RETRY_BACKOFF_SECONDS,
    RTMP_URL,
    SAMPLE_RATE,
    STATE_FILE,
    STREAM_KEY,
    VIDEO_BITRATE,
    RUN_MINUTES,
    VIDEO_BUFSIZE,
    VIDEO_MAXRATE,
    WIDTH,
)

LOG_DIR = ROOT_DIR / "logs" / "live"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | live | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "live.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("quran-live")

STOP = False


def handle_signal(signum, _frame):
    global STOP
    STOP = True
    log.info("Received signal %s; shutting down cleanly...", signum)


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


SURAH_MAP = {row[0]: row for row in SURAHS}


def validate_environment() -> None:
    if not STREAM_KEY:
        raise RuntimeError(
            "YOUTUBE_STREAM_KEY is not set. Create a YouTube live stream and "
            "export its stream key as YOUTUBE_STREAM_KEY."
        )
    if not BACKGROUND_FILE.exists():
        raise RuntimeError(f"Missing livestream background: {BACKGROUND_FILE}")

    if BACKGROUND_FILE.suffix.lower() != ".mp4":
        raise RuntimeError(
            f"Livestream background must be an MP4 video: {BACKGROUND_FILE}"
        )

    for binary in ("ffmpeg", "ffprobe"):
        if subprocess.run(
            ["bash", "-lc", f"command -v {binary} >/dev/null 2>&1"],
            capture_output=True,
        ).returncode != 0:
            raise RuntimeError(f"{binary} is required but was not found in PATH.")

    # Fail before the Quran loop if the background cannot be decoded.
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height",
            "-of", "default=noprint_wrappers=1",
            str(BACKGROUND_FILE),
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0 or "codec_name=" not in probe.stdout:
        raise RuntimeError(
            "FFmpeg could not decode live/background.mp4. "
            f"ffprobe: {probe.stderr.strip()[-1000:]}"
        )

    required_files = (
        ARABIC_FONT_FILE,
        ENGLISH_FONT_FILE,
        ARABIC_DATA_FILE,
        ENGLISH_DATA_FILE,
    )
    for path in required_files:
        if not path.exists():
            raise RuntimeError(f"Missing livestream subtitle asset: {path}")


def load_state() -> tuple[int, int]:
    """Return the NEXT ayah to play, not the last completed ayah."""
    if not STATE_FILE.exists():
        return 1, 1
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        surah = int(data.get("next_surah", 1))
        ayah = int(data.get("next_ayah", 1))
        if surah not in SURAH_MAP:
            return 1, 1
        if ayah < 1 or ayah > SURAH_MAP[surah][3]:
            return 1, 1
        return surah, ayah
    except Exception as exc:
        log.warning("Could not read live state (%s); starting at 1:1.", exc)
        return 1, 1


def save_state(next_surah: int, next_ayah: int, completed_surah: int, completed_ayah: int) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "next_surah": next_surah,
        "next_ayah": next_ayah,
        "last_completed_surah": completed_surah,
        "last_completed_ayah": completed_ayah,
        "updated_at_epoch": int(time.time()),
        "reciter": RECITER_NAME,
    }
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def next_position(surah: int, ayah: int) -> tuple[int, int]:
    total = SURAH_MAP[surah][3]
    if ayah < total:
        return surah, ayah + 1
    next_surah = surah + 1 if surah < 114 else 1
    return next_surah, 1


def audio_url(surah: int, ayah: int) -> str:
    return f"{EVERYAYAH_BASE}/{RECITER_FOLDER}/{surah:03d}{ayah:03d}.mp3"


def cached_path(surah: int, ayah: int) -> Path:
    return CACHE_DIR / f"ayah_{surah:03d}_{ayah:03d}.mp3"


def download_ayah(surah: int, ayah: int) -> Path:
    """Download one ayah with retries. Never silently skips an ayah."""
    dest = cached_path(surah, ayah)
    if dest.exists() and dest.stat().st_size >= 1024:
        return dest

    url = audio_url(surah, ayah)
    last_exc = None

    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        if STOP:
            raise KeyboardInterrupt
        try:
            log.info(
                "Downloading %d:%d (%s) attempt %d/%d",
                surah, ayah, SURAH_MAP[surah][1], attempt, DOWNLOAD_RETRIES,
            )
            with requests.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True) as response:
                response.raise_for_status()
                tmp = dest.with_suffix(".part")
                with tmp.open("wb") as fh:
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            fh.write(chunk)
                if tmp.stat().st_size < 1024:
                    raise RuntimeError("downloaded MP3 is suspiciously small")
                tmp.replace(dest)
            return dest
        except Exception as exc:
            last_exc = exc
            log.warning("Download failed for %d:%d: %s", surah, ayah, exc)
            if attempt < DOWNLOAD_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    raise RuntimeError(f"Could not download {surah}:{ayah}: {last_exc}")


def decode_to_pcm(mp3: Path) -> subprocess.Popen:
    """
    Decode one MP3 to 48kHz stereo signed 16-bit PCM.

    The caller reads stdout and feeds it to FFmpeg's live input. This avoids
    creating a huge full-Quran audio file.
    """
    return subprocess.Popen(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(mp3),
            "-vn",
            "-ar", str(SAMPLE_RATE),
            "-ac", str(CHANNELS),
            "-f", "s16le",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )


def _ffmpeg_filter_path(path: Path) -> str:
    """Escape a Unix path for FFmpeg filter syntax."""
    return str(path).replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")


def start_encoder() -> subprocess.Popen:
    """
    One FFmpeg encoder for the entire live session.

    The background is an MP4, so `-stream_loop -1` is used. The Arabic and
    English subtitle files are reloaded by drawtext while the encoder remains
    alive; Python replaces those files at each ayah boundary.
    """
    rtmp_target = RTMP_URL.rstrip("/") + "/" + STREAM_KEY

    arabic_file, english_file, meta_file = subtitle_files()
    for p in (arabic_file, english_file, meta_file):
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text("", encoding="utf-8")

    bg = _ffmpeg_filter_path(BACKGROUND_FILE)
    ar_file = _ffmpeg_filter_path(arabic_file)
    en_file = _ffmpeg_filter_path(english_file)
    meta = _ffmpeg_filter_path(meta_file)
    ar_font = _ffmpeg_filter_path(ARABIC_FONT_FILE)
    en_font = _ffmpeg_filter_path(ENGLISH_FONT_FILE)

    # Keep all subtitle styling inside FFmpeg so the background video itself
    # remains untouched.
    vf = (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
        # dark translucent panel behind subtitles
        "drawbox=x=60:y=720:w=1800:h=290:color=black@0.55:t=fill,"
        # metadata
        f"drawtext=fontfile='{en_font}':textfile='{meta}':reload=1:"
        "fontcolor=white:fontsize=30:x=(w-text_w)/2:y=735:"
        "box=0:shadowcolor=black@0.9:shadowx=2:shadowy=2,"
        # Arabic
        f"drawtext=fontfile='{ar_font}':textfile='{ar_file}':reload=1:"
        "text_shaping=1:fontcolor=white:fontsize=52:"
        "x=(w-text_w)/2:y=785:line_spacing=10:"
        "shadowcolor=black@0.95:shadowx=3:shadowy=3,"
        # English
        f"drawtext=fontfile='{en_font}':textfile='{en_file}':reload=1:"
        "fontcolor=white:fontsize=30:x=(w-text_w)/2:y=925:"
        "line_spacing=8:shadowcolor=black@0.95:shadowx=2:shadowy=2"
    )

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-re",
        "-stream_loop", "-1",
        "-i", str(BACKGROUND_FILE),
        "-re",
        "-f", "s16le",
        "-ar", str(SAMPLE_RATE),
        "-ac", str(CHANNELS),
        "-i", "pipe:0",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-vf", vf,
        "-r", str(FPS),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-b:v", VIDEO_BITRATE,
        "-maxrate", VIDEO_MAXRATE,
        "-bufsize", VIDEO_BUFSIZE,
        "-g", str(FPS * GOP_SECONDS),
        "-keyint_min", str(FPS * GOP_SECONDS),
        "-sc_threshold", "0",
        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,
        "-ar", str(SAMPLE_RATE),
        "-ac", "2",
        "-f", "flv",
        rtmp_target,
    ]

    # Never log the command: it contains the secret stream key.
    log.info(
        "Starting FFmpeg encoder: %dx%d @ %dfps, %s video / %s audio",
        WIDTH, HEIGHT, FPS, VIDEO_BITRATE, AUDIO_BITRATE,
    )
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=None,
        bufsize=0,
    )


def feed_ayah(encoder: subprocess.Popen, surah: int, ayah: int) -> None:
    mp3 = download_ayah(surah, ayah)
    decoder = decode_to_pcm(mp3)
    assert decoder.stdout is not None
    assert encoder.stdin is not None

    try:
        while not STOP:
            chunk = decoder.stdout.read(64 * 1024)
            if not chunk:
                break
            encoder.stdin.write(chunk)
    finally:
        if decoder.poll() is None:
            decoder.terminate()
            try:
                decoder.wait(timeout=3)
            except subprocess.TimeoutExpired:
                decoder.kill()
        stderr = decoder.stderr.read().decode("utf-8", errors="replace") if decoder.stderr else ""
        if decoder.returncode not in (0, None) and not STOP:
            raise RuntimeError(
                f"FFmpeg decode failed for {surah}:{ayah}: {stderr[-500:]}"
            )

    # Successful playback means we can remove the rolling cache file.
    if not KEEP_PLAYED_AUDIO:
        try:
            mp3.unlink(missing_ok=True)
        except OSError:
            pass


def run_stream() -> None:
    validate_environment()

    deadline = (time.monotonic() + RUN_MINUTES * 60) if RUN_MINUTES > 0 else None
    if deadline:
        log.info("Bounded run enabled: %d minutes", RUN_MINUTES)

    surah, ayah = load_state()
    log.info(
        "Starting Quran livestream — reciter=%s — starting at %d:%d (%s)",
        RECITER_NAME, surah, ayah, SURAH_MAP[surah][1],
    )
    log.info("Background: %s", BACKGROUND_FILE)

    encoder: Optional[subprocess.Popen] = None
    consecutive_encoder_failures = 0

    while not STOP:
        if deadline is not None and time.monotonic() >= deadline:
            log.info("Run window reached; stopping after the completed ayah.")
            break

        if encoder is None or encoder.poll() is not None:
            if encoder is not None:
                log.warning(
                    "FFmpeg exited with code %s; restarting encoder.",
                    encoder.returncode,
                )
            if consecutive_encoder_failures >= MAX_CONSECUTIVE_FFMPEG_FAILURES:
                raise RuntimeError(
                    "FFmpeg exceeded the consecutive failure limit; "
                    "check the live log and YouTube ingest settings."
                )
            if encoder is not None:
                time.sleep(RESTART_BACKOFF_SECONDS)
            encoder = start_encoder()
            consecutive_encoder_failures += 1

        try:
            log.info(
                "LIVE %d:%d — %s / %s",
                surah, ayah, SURAH_MAP[surah][1], SURAH_MAP[surah][2],
            )
            prepare_subtitles(
                surah,
                ayah,
                SURAH_MAP[surah][1],
                SURAH_MAP[surah][3],
            )
            feed_ayah(encoder, surah, ayah)

            if encoder.poll() is not None:
                # We did not complete a valid live write. Do not advance state.
                raise RuntimeError(
                    f"Encoder stopped during {surah}:{ayah} (exit={encoder.returncode})"
                )

            next_surah, next_ayah = next_position(surah, ayah)
            save_state(next_surah, next_ayah, surah, ayah)

            # Only now advance. This prevents a failed decoder/encoder from
            # silently skipping a Quran ayah after a restart.
            surah, ayah = next_surah, next_ayah
            consecutive_encoder_failures = 0

            if deadline is not None and time.monotonic() >= deadline:
                log.info("Run window reached after %d:%d; exiting cleanly.", surah, ayah)
                break

        except BrokenPipeError:
            log.warning("YouTube/FFmpeg pipe closed during %d:%d; restarting.", surah, ayah)
            if encoder and encoder.poll() is None:
                encoder.kill()
            encoder = None
            time.sleep(RESTART_BACKOFF_SECONDS)

        except KeyboardInterrupt:
            break

        except Exception as exc:
            log.exception("Live stream error at %d:%d: %s", surah, ayah, exc)
            if encoder and encoder.poll() is None:
                encoder.kill()
            encoder = None
            # Do not advance the state; retry the same ayah.
            time.sleep(RESTART_BACKOFF_SECONDS)

    if encoder is not None and encoder.poll() is None:
        try:
            encoder.stdin.close()
        except Exception:
            pass
        encoder.terminate()
        try:
            encoder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            encoder.kill()
    log.info("Quran livestream stopped.")


if __name__ == "__main__":
    run_stream()
