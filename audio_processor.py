#!/usr/bin/env python3
"""
audio_processor.py
Broadcast-style audio finishing for the concatenated recitation track:
gentle rumble removal, subtle warmth/presence EQ, light compression,
accurate two-pass EBU R128 loudness normalization, a safety limiter, and
fade in/out. Duration is never touched, so sync with the (externally
timed) subtitles is never affected — only tone and levels change.

Processing chain (in order):
  1. Denoise            — a light, conservative spectral denoiser that
     lifts the noise floor (room hiss, handheld-mic self-noise, faint
     background hum) without introducing the "underwater"/warbly
     artifacts that aggressive noise reduction causes. Intentionally
     mild: this is cleanup, not restoration.
  2. High-pass filter (single stage) — removes sub-70Hz rumble/handling
     noise. An earlier version of this chain cascaded two stages (50Hz
     2-pole + 90Hz 1-pole), which combine into an effective ~18dB/octave
     rolloff reaching well up into the 90-120Hz range — exactly where a
     mid-to-low register reciter's vocal fundamental and chest resonance
     live, thinning out the "warm/deep" quality this mastering is
     supposed to add. A single 12dB/octave stage at 70Hz clears rumble
     and handling noise just as effectively for a voice-only source
     (there's no bass instrument to protect against) while leaving the
     entire vocal fundamental and its harmonics untouched.
  3. Warmth EQ           — a small, fairly narrow low-mid lift for body/
     warmth, paired with a small wide cut just above it to stop the
     naturally dominant low-mid band (this is where most of a reciter's
     vocal energy already lives) from building into mud once compression
     and makeup gain stack on top of it.
  4. Presence EQ         — a small, broad lift in the upper-mids for
     clarity/intelligibility on phone speakers, plus a very gentle high
     shelf for "air"/openness on genuinely wideband sources. Kept
     deliberately gentle and broad (not peaky) — this band is also where
     harshness and sibilance live, and the goal is clarity, not an
     obviously "processed" edge. The shelf is a no-op on heavily
     compressed/lossy sources (there's nothing left up there to lift) —
     it only helps when the source actually has it.
  5. Harmonic exciter    — a subtle, mostly-dry blend of upper harmonics.
     Used sparingly (low amount, mostly-dry blend) because it's the
     filter most likely to tip a "premium/open" voice into an
     "artificial/metallic" one if pushed — it works differently from EQ:
     instead of boosting an existing frequency band, it adds new,
     quieter harmonic content derived from the signal itself. Like the
     air shelf, it has diminishing returns on a heavily transcoded
     source, since there's less genuine harmonic detail to draw from.
  6. Parallel compression — the original ("dry") signal is blended with a
     more heavily compressed ("wet") copy of itself, rather than sending
     100% of the signal through one compressor. This is the classic
     mastering-bus trick for adding density/loudness-readiness/"glue"
     while the dry path keeps the natural transients and breath dynamics
     that a single hard compressor would flatten.
  7. De-esser            — targeted, dynamic reduction of sibilance
     ("s"/"sh" harshness) in the 5-8kHz region. Arabic recitation carries
     a lot of inherently sibilant content (seen/sheen/saad/zay) even on
     an untouched source, so some de-essing earns its place regardless;
     it's tuned gently here since the presence EQ and exciter above are
     both kept low enough that they're no longer the main driver of it.
  8. Loudnorm (2-pass)   — the chain above is measured first, then
     applied as a single linear gain move to the target LUFS. This is
     the standard "accurate" loudnorm mode: it avoids the audible
     gain-riding/pumping that the single-pass dynamic mode can produce,
     at the cost of one extra (cheap, encode-free) analysis pass.
  9. Limiter              — a true-peak safety net with a true-peak
     ceiling below the platforms' own encoders (see AUDIO_TRUE_PEAK/
     LIMITER_CEILING) to avoid inter-sample clipping after re-encoding
     by Instagram/YouTube/Facebook, with a release time slow enough that
     gain recovery after a peak isn't audible as "breathing."
  10. Fade in/out, using perceptually-smooth curves (quarter-sine in,
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


def _pre_dynamics_graph(in_label: str, out_label: str) -> str:
    """
    The tone-shaping + dynamics portion of the chain, as a filter_complex
    graph fragment. Shared by both the loudnorm measurement pass and the
    final render pass so the two can never drift out of sync with each
    other.

    Takes the graph from `in_label` (e.g. "[0:a]") through denoise, EQ,
    exciter, parallel compression and de-essing, and leaves the result
    on `out_label` (e.g. "[shaped]"), ready for loudnorm/limiter/fades.

    All gains here are intentionally small — this is meant to be heard
    as "clean and natural," not "processed."
    """
    return (
        f"{in_label}"
        # Light spectral denoise: raises the noise floor gently (nr=8,
        # a conservative reduction amount) and only treats material below
        # -40dB as noise (nf=-40), so it cleans handheld-mic hiss/room
        # tone without touching the reciter's voice or breaths, and
        # without the "underwater" artifacting that aggressive settings
        # cause.
        "afftdn=nr=8:nf=-40:tn=1,"
        # Rumble/handling-noise removal. A single, well-placed 12dB/octave
        # stage at 70Hz clears sub-sonic noise cleanly for a voice-only
        # source (no bass instrument to protect against), while staying
        # safely below the fundamental of even a low/mid-register
        # reciter's voice — an earlier two-stage version (50Hz 2-pole +
        # 90Hz 1-pole) combined into a steeper rolloff that reached into
        # the 90-120Hz range and measurably thinned out warmth/depth.
        "highpass=f=70:poles=2,"
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
        # intelligibility on phone speakers. Kept low-gain and broad
        # (wide bandwidth, not a narrow peak) — a peakier lift here reads
        # as "papery"/harsh on a recitation and compounds with the
        # exciter below into exactly the processed/edgy character this
        # master is meant to avoid.
        "equalizer=f=4000:t=q:w=0.8:g=1.0,"
        # Air/openness: a very gentle high shelf. This is a genuine no-op
        # on a heavily compressed or previously lossy-transcoded source
        # (there's nothing left up there to lift), but gives a real,
        # high-quality recording a touch of open clarity without ever
        # approaching brightness or harshness.
        "treble=f=9000:width_type=o:width=1.0:g=1.0,"
        # Harmonic exciter, kept deliberately subtle (amount=0.35, mostly
        # dry blend at -9dB): adds a light touch of upper-harmonic
        # "shimmer" rather than boosting an existing band. The original
        # amount=0.6/drive=8.5/blend=-6dB setting was audibly on the edge
        # of "processed" — this is voice-only content with no other
        # instruments to hide behind, so any hint of synthetic harmonic
        # buildup is immediately noticeable. Turned down to where it's
        # felt as openness rather than heard as an effect.
        "aexciter=amount=0.35:drive=6.0:blend=-9,"
        f"asplit=2[{out_label}_dry][{out_label}_presplit];"
        # Parallel ("New York") compression: the wet path is squeezed
        # noticeably harder than a single serial compressor would be
        # (lower threshold, higher ratio), because it's only ever heard
        # blended underneath the dry signal. This adds density/glue and
        # loudness-readiness while the dry path preserves the reciter's
        # natural transients, breaths and pauses untouched.
        f"[{out_label}_presplit]acompressor=threshold=-28dB:ratio=4:attack=15:release=280:knee=8:makeup=3[{out_label}_wet];"
        f"[{out_label}_dry][{out_label}_wet]amix=inputs=2:weights=1.0 0.5:normalize=0,"
        # De-esser: targeted, dynamic reduction in the sibilance range.
        # Still needed because Arabic recitation carries a lot of
        # sibilant content on its own (seen/sheen/saad/zay), independent
        # of any EQ — but with the presence lift and exciter both scaled
        # back above, less correction is needed to keep it natural.
        f"deesser=i=0.3:m=0.5:f=0.5:s=o[{out_label}]"
    )


def _measure_loudness(src: Path, pre_graph: str, target_lufs: float, target_tp: float):
    """
    First pass: runs the tone/dynamics graph + loudnorm in measurement
    mode (no output file) to get the real input loudness stats. Returns
    the parsed stats dict, or None if measurement/parsing fails (caller
    falls back to single-pass dynamic loudnorm so mastering never breaks
    the pipeline over this).
    """
    filter_complex = (
        f"{pre_graph};"
        f"[shaped]loudnorm=I={target_lufs}:TP={target_tp}:LRA=7:print_format=json[out]"
    )
    cmd = ["ffmpeg", "-y", "-i", str(src), "-filter_complex", filter_complex,
           "-map", "[out]", "-f", "null", "-"]
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
    pre_graph = _pre_dynamics_graph(in_label="[0:a]", out_label="shaped")

    stats = _measure_loudness(src, pre_graph, AUDIO_TARGET_LUFS, AUDIO_TRUE_PEAK)

    if stats:
        loudnorm = (
            f"loudnorm=I={AUDIO_TARGET_LUFS}:TP={AUDIO_TRUE_PEAK}:LRA=7:"
            f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
            f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:"
            f"offset={stats.get('target_offset', 0)}:linear=true:print_format=summary"
        )
    else:
        loudnorm = f"loudnorm=I={AUDIO_TARGET_LUFS}:TP={AUDIO_TRUE_PEAK}:LRA=7:print_format=summary"

    filter_complex = (
        f"{pre_graph};"
        f"[shaped]{loudnorm},"
        f"alimiter=limit={LIMITER_CEILING}:attack=5:release=100,"
        f"afade=t=in:st=0:d={AUDIO_FADE_IN}:curve=qsin,"
        f"afade=t=out:st={fade_out_start:.2f}:d={AUDIO_FADE_OUT}:curve=hsin[out]"
    )

    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        # 256k + the two-loop coder gives a cleaner final encode than the
        # default fast/short-term coder, worth the modest extra encode
        # time for a "premium" master; 48kHz is already full quality for
        # a voice-only source.
        "-c:a", "aac", "-b:a", "256k", "-aac_coder", "twoloop", "-ar", "48000",
        str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Audio mastering failed: {result.stderr[-400:]}")
    log.info(
        "Audio mastered -> %s (target %.1f LUFS, %s loudnorm)",
        dst.name, AUDIO_TARGET_LUFS, "2-pass" if stats else "1-pass fallback",
    )
