#!/usr/bin/env python3
"""
audio_processor.py
Broadcast-style audio finishing for the concatenated recitation track:
EBU R128 loudness normalization, gentle compression, a brick-wall
limiter, and fade in/out. Runs as a single ffmpeg pass so sync with
the (externally-timed) subtitles is never touched — only levels change.
"""

import subprocess
from pathlib import Path

from config import AUDIO_TARGET_LUFS, AUDIO_TRUE_PEAK, AUDIO_FADE_IN, AUDIO_FADE_OUT
from logging_utils import get_logger

log = get_logger(__name__)


def master_audio(src: Path, dst: Path, total_duration: float) -> None:
    """
    Applies (in order): gentle compression to even out recitation dynamics,
    loudnorm to a consistent target LUFS for social platforms, a true-peak
    limiter to prevent clipping, and fade in/out. Duration is untouched,
    so word/ayah timing used by the subtitle track remains valid.
    """
    fade_out_start = max(total_duration - AUDIO_FADE_OUT, 0)

    af = (
        f"acompressor=threshold=-18dB:ratio=2.5:attack=20:release=250,"
        f"loudnorm=I={AUDIO_TARGET_LUFS}:TP={AUDIO_TRUE_PEAK}:LRA=11:print_format=summary,"
        f"alimiter=limit=0.95:attack=5:release=50,"
        f"afade=t=in:st=0:d={AUDIO_FADE_IN},"
        f"afade=t=out:st={fade_out_start:.2f}:d={AUDIO_FADE_OUT}"
    )

    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-af", af,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Audio mastering failed: {result.stderr[-400:]}")
    log.info("Audio mastered -> %s (target %.1f LUFS)", dst.name, AUDIO_TARGET_LUFS)
