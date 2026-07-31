#!/usr/bin/env python3
"""
audio_processor.py
Broadcast-style audio finishing for the concatenated recitation track:
gentle rumble removal, subtle warmth/presence EQ, light compression,
accurate two-pass EBU R128 loudness normalization, a safety limiter, and
fade in/out. Duration is never touched, so sync with the (externally
timed) subtitles is never affected — only tone and levels change.

Processing chain (in order):
  1. High-pass filter (two cascaded stages) — removes sub-90Hz rumble/
     handling noise that sits below the reciter's vocal fundamental,
     without thinning the voice. A single 12dB/oct stage at 45Hz left a
     soft shoulder of low-end mud between ~45-90Hz; a firm 50Hz stage
     plus a gentle extra 90Hz trim clears that region cleanly.
  2. Warmth EQ           — a small, fairly narrow low-mid lift for body/
     warmth, paired with a small wide cut just above it to stop the
     naturally dominant low-mid band (this is where most of a reciter's
     vocal energy already lives) from building into mud once compression
     and makeup gain stack on top of it.
  3. Presence EQ         — a small, broad lift in the upper-mids for
     clarity/intelligibility on phone speakers, plus a very gentle high
     shelf for "air"/openness on genuinely wideband sources. The shelf is
     a no-op on heavily compressed/lossy sources (there's nothing left up
     there to lift) — it only helps when the source actually has it.
  4. Compressor          — gentle dynamic control tuned for spoken-word
     pacing (slow attack/release, soft knee) so tajweed dynamics, breaths
     and pauses stay natural instead of being flattened, pumped, or
     audibly "grabbed" at the onset.
  5. Loudnorm (2-pass)   — the chain above is measured first, then
     applied as a single linear gain move to the target LUFS. This is
     the standard "accurate" loudnorm mode: it avoids the audible
     gain-riding/pumping that the single-pass dynamic mode can produce,
     at the cost of one extra (cheap, encode-free) analysis pass.
  6. Limiter              — a true-peak safety net with a true-peak
     ceiling below the platforms' own encoders (see AUDIO_TRUE_PEAK/
     LIMITER_CEILING) to avoid inter-sample clipping after re-encoding
     by Instagram/YouTube/Facebook, with a release time slow enough that
     gain recovery after a peak isn't audible as "breathing."
  7. Fade in/out, using perceptually-smooth curves (quarter-sine in,
     half-sine out) instead of a linear ramp, so neither end of the clip
     reads as an abrupt on/off switch.

None of these steps alter pitch, formants, or the reciter's natural
timbre — this is level and tone shaping only, not voice modification.

A NOTE ON SOURCE QUALITY
-------------------------
This module can only polish what's in the source file. If the source
recitation itself is low bitrate, was captured on a noisy/handheld mic,
already clipped, or was previously heavily compressed/re-encoded (for
example, forwarded through WhatsApp, which transcodes audio down to
low-bitrate HE-AAC and typically rolls off nearly everything above
~5kHz), no amount of EQ or dynamics processing here can recover lost
fidelity — pushing harder on DSP to compensate for a poor source tends
to make audible artifacts (harshness, pumping, noise) worse, not better.
If the mastered output still sounds thin, harsh, dull, or noisy after
this chain, the fix is a better-quality source recording (ideally a
direct export from the recording device/app rather than anything that
has passed through a messaging app's compression), not more processing.
"""

import json
import re
import subprocess
from pathlib import Path

from config import AUDIO_TARGET_LUFS, AUDIO_TRUE_PEAK, AUDIO_FADE_IN, AUDIO_FADE_OUT
from logging_utils import get_logger

log = get_logger(__name__)

# True-peak ceiling for the final limiter, expressed as a linear sample
# value (0.0-1.0). Kept below AUDIO_TRUE_PEAK's dBTP target with margin:
# social platforms re-encode audio (AAC/Opus) after upload, and
# re-encoding can introduce inter-sample peaks a little above the
# original file's true peak. Limiting to ~-1dBTP (0.891 linear) instead
# of shaving it razor-close to 0dBFS leaves headroom for that.
LIMITER_CEILING = 0.891


def _pre_dynamics_chain() -> str:
    """
    The tone-shaping + dynamics portion of the chain, shared by both the
    loudnorm measurement pass and the final render pass so the two can
    never drift out of sync with each other.

    All gains here are intentionally small — this is meant to be heard
    as "clean and natural," not "processed."
    """
    return (
        # Rumble/handling-noise removal, in two cascaded stages rather
        # than one. A single 45Hz/12dB-per-octave stage still leaves a
        # soft shoulder of low-end energy between roughly 45-90Hz. A
        # firmer 50Hz stage clears the floor decisively (well below any
        # reciter's vocal fundamental), and a gentle extra 90Hz stage
        # trims the remaining shoulder without touching the fundamental
        # itself.
        "highpass=f=50:poles=2,"
        "highpass=f=90:poles=1,"
        # Warmth: a small, fairly narrow lift in the low-mids (body/chest
        # resonance). Kept narrower and slightly lower gain than a broad
        # boost, since a reciter's vocal fundamental already carries most
        # of the track's energy in this region — this adds a touch of
        # body without over-thickening an already-warm voice.
        "equalizer=f=200:t=q:w=0.8:g=1.2,"
        # Mud control: a small, wide cut just above the warmth band. The
        # low-mids are naturally the most energy-dense part of a
        # recitation; without this, the warmth boost plus the
        # compressor's makeup gain can stack into a boxy/muddy build-up
        # here. This keeps "warm" from sliding into "muddy."
        "equalizer=f=400:t=q:w=1.5:g=-1.0,"
        # Presence: a small, broad lift in the upper-mids for
        # intelligibility on phone speakers, kept subtle to avoid any
        # hint of harshness or sibilance exaggeration.
        "equalizer=f=4000:t=q:w=1.0:g=1.5,"
        # Air/openness: a very gentle high shelf. This is a genuine no-op
        # on a heavily compressed or previously lossy-transcoded source
        # (there's nothing left up there to lift), but gives a real,
        # high-quality recording a touch of open clarity without ever
        # approaching brightness or harshness.
        "treble=f=9000:width_type=o:width=1.0:g=1.0,"
        # Gentle leveling: slow attack/release preserves tajweed
        # dynamics, breaths and natural pauses; a soft knee (instead of
        # the default hard knee) makes the compressor's onset smooth and
        # inaudible rather than an audible "grab" on louder emphasis.
        "acompressor=threshold=-20dB:ratio=2.2:attack=25:release=320:knee=6:makeup=1"
    )


def _measure_loudness(src: Path, pre_chain: str, target_lufs: float, target_tp: float):
    """
    First pass: runs the tone/dynamics chain + loudnorm in measurement
    mode (no output file) to get the real input loudness stats. Returns
    the parsed stats dict, or None if measurement/parsing fails (caller
    falls back to single-pass dynamic loudnorm so mastering never breaks
    the pipeline over this).
    """
    af = f"{pre_chain},loudnorm=I={target_lufs}:TP={target_tp}:LRA=7:print_format=json"
    cmd = ["ffmpeg", "-y", "-i", str(src), "-af", af, "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True)

    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", result.stderr, re.S)
    if not match:
        log.warning("Loudnorm measurement pass produced no stats; falling back to single-pass normalization")
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        log.warning("Could not parse loudnorm measurement stats; falling back to single-pass normalization")
        return None


def master_audio(src: Path, dst: Path, total_duration: float) -> None:
    """
    Applies (in order): high-pass rumble removal, subtle warmth/presence/
    air EQ, gentle soft-knee compression, accurate two-pass loudness
    normalization to a consistent target LUFS for social platforms, a
    true-peak safety limiter, and perceptually-smooth fade in/out.
    Duration is untouched, so word/ayah timing used by the subtitle track
    remains valid.
    """
    fade_out_start = max(total_duration - AUDIO_FADE_OUT, 0)
    pre_chain = _pre_dynamics_chain()

    stats = _measure_loudness(src, pre_chain, AUDIO_TARGET_LUFS, AUDIO_TRUE_PEAK)

    if stats:
        # Accurate mode: apply the measured stats as a single linear gain
        # move, rather than the dynamic (gain-riding) algorithm. This is
        # the cleaner-sounding option and is what "print_format=json"
        # measurement passes exist to feed. LRA=7 targets a tighter,
        # more confidently "broadcast-consistent" dynamic range than the
        # generic default of 11, while still leaving real headroom for
        # natural pauses and emphasis (unlike the reference clip's
        # measured 2.9 LRA, which reflects a short WhatsApp-compressed
        # sample rather than a realistic target for a full reel).
        loudnorm = (
            f"loudnorm=I={AUDIO_TARGET_LUFS}:TP={AUDIO_TRUE_PEAK}:LRA=7:"
            f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
            f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:"
            f"offset={stats.get('target_offset', 0)}:linear=true:print_format=summary"
        )
    else:
        # Fallback: single-pass dynamic loudnorm (previous behavior).
        loudnorm = f"loudnorm=I={AUDIO_TARGET_LUFS}:TP={AUDIO_TRUE_PEAK}:LRA=7:print_format=summary"

    af = (
        f"{pre_chain},"
        f"{loudnorm},"
        # Release lengthened slightly from 80ms to 100ms so gain recovery
        # after a limited peak isn't perceptible as "breathing"; attack
        # stays fast (5ms) since that's what reliably catches true peaks
        # before platform re-encoding.
        f"alimiter=limit={LIMITER_CEILING}:attack=5:release=100,"
        # Perceptually-smooth fade curves: loudness is perceived roughly
        # logarithmically, so a linear ramp (the previous default) can
        # read as an abrupt on/off rather than a smooth rise/tail.
        f"afade=t=in:st=0:d={AUDIO_FADE_IN}:curve=qsin,"
        f"afade=t=out:st={fade_out_start:.2f}:d={AUDIO_FADE_OUT}:curve=hsin"
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
    log.info(
        "Audio mastered -> %s (target %.1f LUFS, %s loudnorm)",
        dst.name, AUDIO_TARGET_LUFS, "2-pass" if stats else "1-pass fallback",
    )
