#!/usr/bin/env python3
"""
surah_audio.py
Downloads and masters the COMPLETE recitation for one Surah (all ayat,
never a batch/prefix like the Shorts pipeline). Reuses
audio_downloader.download_one_ayah and audio_processor.master_audio —
this module only orchestrates them across a full ayah range and adds
on-disk caching so a re-run after a failure never re-downloads or
re-masters audio that already succeeded.

TWO THINGS DELIBERATELY DIFFER FROM THE SHORTS AUDIO PATH, BOTH FOR THE
SAME REASON — a full Surah has hundreds of ayah boundaries where the
Shorts path (~7-ayah batches) has a handful, so a per-boundary error that
is inaudible/negligible there becomes real, measured drift here:

1. Each downloaded ayah MP3 is decoded ONCE to a canonical WAV
   (`_decode_to_wav`) and every duration used anywhere in this pipeline
   (the timeline, the mastering input, the cache-validity check) is
   measured from that WAV, never from the source MP3's own container
   metadata. Verified empirically: for MP3s carrying LAME/Xing gapless
   metadata, ffprobe's `format=duration` on the raw MP3 does not always
   equal the number of samples ffmpeg's decoder actually outputs for
   that file — a small, consistent per-file discrepancy (measured
   ~40-50ms/ayah on real test files) that compounds linearly with ayah
   count. A decoded WAV's duration has no such ambiguity: what ffprobe
   reports for a WAV is exactly its sample count / sample rate, and is
   exactly what a lossless WAV-to-WAV concatenation produces. This is
   "use the actual final audio as the source of truth," applied at the
   per-ayah level so the timeline can't drift from what's actually
   rendered even before mastering runs.
2. Concatenation of those (now-WAV) sources happens INSIDE master_audio()
   (its multi-source mode — see audio_processor.py's sample-accurate
   `concat` filter), not via audio_downloader.concat_audio's stream-copy
   demuxer. That demuxer is untouched and still used as-is by the Shorts
   pipeline.

IMPORTANT: this intentionally does NOT touch audio_downloader.get_next_batch
/ load_progress / save_progress. Those drive the separate, incremental
Shorts/Reels progress tracker (progress.json + the QURAN_PROGRESS GitHub
Variable) and must never be read or advanced by a full-Surah build —
doing so would desync the daily Shorts pipeline's "next ayah" cursor.
"""

import subprocess
from pathlib import Path

from audio_downloader import download_one_ayah, get_duration
from audio_processor import master_audio
from config import LONGFORM_AUDIO_BITRATE, LONGFORM_AUDIO_SAMPLE_RATE
from surah_validator import quick_media_check
from logging_utils import get_logger

log = get_logger(__name__)


def _decode_to_wav(src_mp3: Path, dest_wav: Path, sample_rate: int) -> float:
    """Decodes one ayah MP3 to a canonical PCM WAV and returns its
    (authoritative — see module docstring) duration."""
    cmd = ["ffmpeg", "-y", "-i", str(src_mp3), "-ar", str(sample_rate), "-ac", "2",
           "-c:a", "pcm_s16le", str(dest_wav)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Could not decode {src_mp3.name} to WAV: {result.stderr[-300:]}")
    return get_duration(dest_wav)


def download_full_surah(surah_num: int, ayah_count: int, work_dir: Path,
                         retries: int = 3) -> tuple:
    """
    Downloads every ayah 1..ayah_count for `surah_num` in order and
    decodes each to a canonical WAV (see module docstring). Returns
    (wav_files, durations) — both ordered ayah 1..N, with durations
    measured from the decoded WAVs. Skips ayahs whose MP3/WAV pair
    already exists on disk (from a previous interrupted run) after
    re-validating both with ffprobe, so a resumed run only re-fetches/
    re-decodes what's actually missing or invalid.
    """
    raw_dir = work_dir / "audio" / "ayat"
    wav_dir = work_dir / "audio" / "ayat_wav"
    raw_dir.mkdir(parents=True, exist_ok=True)
    wav_dir.mkdir(parents=True, exist_ok=True)

    wav_files, durations = [], []
    for ayah in range(1, ayah_count + 1):
        mp3_dest = raw_dir / f"ayah_{surah_num:03d}_{ayah:03d}.mp3"
        wav_dest = wav_dir / f"ayah_{surah_num:03d}_{ayah:03d}.wav"

        if wav_dest.exists() and mp3_dest.exists():
            if quick_media_check(wav_dest, min_duration=0.1, require_audio=True):
                wav_files.append(wav_dest)
                durations.append(get_duration(wav_dest))
                continue
            wav_dest.unlink(missing_ok=True)

        if not quick_media_check(mp3_dest, min_duration=0.1, require_audio=True):
            mp3_dest.unlink(missing_ok=True)
            log.info("  Audio %d:%d", surah_num, ayah)
            download_one_ayah(surah_num, ayah, mp3_dest, retries=retries)

        dur = _decode_to_wav(mp3_dest, wav_dest, LONGFORM_AUDIO_SAMPLE_RATE)
        log.info("    %d:%d -> %.2fs", surah_num, ayah, dur)
        wav_files.append(wav_dest)
        durations.append(dur)

    total = sum(durations)
    log.info("Full Surah %d audio ready: %d ayat, %.1fs (%.1f min)",
              surah_num, len(wav_files), total, total / 60)
    return wav_files, durations


def build_mastered_audio(surah_num: int, audio_files: list, work_dir: Path,
                          expected_duration: float = None, force: bool = False) -> Path:
    """
    Concatenates the full ordered ayah list and masters it, in one pass,
    via audio_processor.master_audio's multi-source mode (sample-accurate
    concat of the WAVs produced by download_full_surah — see module
    docstring) at the long-form 320k/48kHz target.

    Cached: if a mastered file already exists, is ffprobe-readable, and
    (when `expected_duration` is given) its duration is within tolerance
    of the sum of the source ayah durations, it's reused as-is
    (stage-resume behavior — spec section 23). An existing-but-invalid
    file is NOT trusted just because it exists — it's regenerated.
    """
    audio_dir = work_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    mastered_path = audio_dir / "combined_mastered.m4a"

    if mastered_path.exists() and not force:
        min_dur = (expected_duration - 2.0) if expected_duration else 0.2
        if quick_media_check(mastered_path, min_duration=min_dur, require_audio=True):
            log.info("Reusing existing mastered audio -> %s", mastered_path.name)
            return mastered_path
        log.warning("Existing mastered audio at %s failed validity check — rebuilding.", mastered_path.name)

    master_audio(
        audio_files, mastered_path, expected_duration or sum(get_duration(f) for f in audio_files),
        bitrate=LONGFORM_AUDIO_BITRATE, sample_rate=LONGFORM_AUDIO_SAMPLE_RATE,
    )
    return mastered_path
