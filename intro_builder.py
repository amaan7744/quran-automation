#!/usr/bin/env python3
"""
intro_builder.py
Builds the short cinematic opening sequence described in item 1 of the
brief:

    black screen -> faint stars / soft light -> Bismillah -> main video

This module NEVER touches subtitles or the main recitation. The main
video (background + burned-in subtitles + mastered recitation audio) is
built completely independently, exactly as before, with the very same
timing it has always had — see build_video.py. The intro is rendered as
a fully separate short clip and only joined on as the LAST step, with a
short crossfade at the seam. Because subtitle text is already
hard-burned as pixels into the main segment before this join ever
happens, prepending an intro cannot shift, retime, or otherwise affect
subtitle sync in any way.

Audio sourcing follows the strict priority from this pass's brief:
  1. BISMILLAH_AUDIO_PATH, if it points to an existing local file — this
     is the ONLY source used by default, and covers both "your own
     recording" and "a licensed recording" (licensing an audio file
     just means pointing this path at it).
  2. If unavailable, the intro is SKIPPED for this run (logged clearly)
     rather than falling back to anything synthetic — build_video.py
     opens directly on the first verse, exactly as it did before this
     upgrade.

  gTTS-based Arabic TTS is NOT part of this priority order. It exists
  only as an explicit, off-by-default opt-in (BISMILLAH_TTS_ENABLED)
  for anyone who consciously wants a synthesized placeholder voice
  anyway; it is never used automatically, and a production channel
  should not rely on it — see _synthesize_bismillah_tts() below. It
  does not imitate any specific reciter's voice.
"""

import random
import shutil
import subprocess
from pathlib import Path

from config import (
    VIDEO_WIDTH, VIDEO_HEIGHT, INTRO_VISUAL_FPS,
    BISMILLAH_AUDIO_PATH, BISMILLAH_TTS_ENABLED, BISMILLAH_TTS_CACHE_PATH,
    BISMILLAH_TEXT_ARABIC, INTRO_MIN_DURATION, INTRO_TARGET_MAX_DURATION,
    INTRO_HARD_DURATION_CEILING, INTRO_PRE_BLACK, INTRO_AUDIO_FADE_OUT,
    INTRO_AUDIO_SANITY_CEILING, INTRO_JOIN_TRANSITION,
    INTRO_STAR_PROBABILITY, INTRO_STAR_PROBABILITY_JITTER,
    INTRO_STAR_OPACITY_RANGE, INTRO_LIGHT_LIFT_RANGE, AUDIO_TARGET_LUFS,
    FINAL_ENCODE_CRF, FINAL_ENCODE_PRESET, AUDIO_BITRATE, AUDIO_SAMPLE_RATE,
)
from audio_downloader import get_duration
from logging_utils import get_logger

log = get_logger(__name__)


class IntroBuildError(Exception):
    """Raised only for genuine ffmpeg failures while building the intro.
    Missing/unavailable Bismillah audio is NOT an error — it's handled
    by skipping the intro (see build_intro())."""


# ══════════════════════════════════════════════════════════════════════════
# AUDIO SOURCING
# ══════════════════════════════════════════════════════════════════════════

def resolve_bismillah_audio() -> Path | None:
    """Resolves a usable Bismillah audio file. By default, this ONLY
    ever returns BISMILLAH_AUDIO_PATH (your own or a licensed
    recording) or None — see the module docstring for why gTTS is
    deliberately excluded from this priority order by default. Never
    raises for a missing/unusable source — that's an expected, handled
    case, not a bug."""
    local_path = Path(BISMILLAH_AUDIO_PATH)
    if local_path.exists() and local_path.stat().st_size > 0:
        try:
            dur = get_duration(local_path)
        except (subprocess.CalledProcessError, ValueError) as e:
            log.warning("BISMILLAH_AUDIO_PATH exists but isn't a readable audio file (%s) — skipping intro.", e)
            return None
        if dur > INTRO_AUDIO_SANITY_CEILING:
            log.warning(
                "BISMILLAH_AUDIO_PATH is %.1fs long (> %.1fs sanity ceiling) — this looks like the "
                "wrong file (a full reciter track?), not a short Bismillah clip. Skipping intro rather "
                "than building an intro that would badly hurt retention.",
                dur, INTRO_AUDIO_SANITY_CEILING,
            )
            return None
        log.info("Using local Bismillah audio: %s (%.2fs)", local_path, dur)
        return local_path

    if BISMILLAH_TTS_ENABLED:
        log.warning(
            "BISMILLAH_TTS_ENABLED=true — using a synthesized gTTS voice as an explicit opt-in. "
            "This is NOT recommended for a production channel (it sounds obviously synthetic, "
            "not 'deep, calm, warm, reverent'); set a real BISMILLAH_AUDIO_PATH recording instead "
            "when you can."
        )
        cached = _synthesize_bismillah_tts()
        if cached:
            return cached

    log.warning(
        "No Bismillah audio available (BISMILLAH_AUDIO_PATH=%s not found, TTS fallback %s) — "
        "skipping the cinematic intro for this run. The video will open directly on the first "
        "verse, exactly as before this upgrade. Set BISMILLAH_AUDIO_PATH to a short licensed "
        "recording (or set BISMILLAH_TTS_ENABLED=true) to enable it.",
        BISMILLAH_AUDIO_PATH, "enabled but failed" if BISMILLAH_TTS_ENABLED else "disabled",
    )
    return None


def _synthesize_bismillah_tts() -> Path | None:
    """
    Best-effort Arabic TTS fallback using the `gTTS` package (Google
    Translate's TTS endpoint — a generic synthesized voice, not any
    specific reciter). Requires outbound network access and the `gtts`
    package (see requirements.txt). Cached to disk after the first
    successful synthesis so repeat runs don't re-hit the network.
    Returns None (never raises) on any failure — a missing/failed TTS
    fallback should degrade to "no intro," not break the whole render.
    """
    if BISMILLAH_TTS_CACHE_PATH.exists() and BISMILLAH_TTS_CACHE_PATH.stat().st_size > 0:
        log.info("Using cached Bismillah TTS audio: %s", BISMILLAH_TTS_CACHE_PATH)
        return BISMILLAH_TTS_CACHE_PATH

    try:
        from gtts import gTTS
    except ImportError:
        log.warning("BISMILLAH_TTS_ENABLED=true but the `gtts` package isn't installed "
                    "(pip install gtts) — cannot synthesize a fallback.")
        return None

    try:
        BISMILLAH_TTS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tts = gTTS(text=BISMILLAH_TEXT_ARABIC, lang="ar", slow=False)
        tts.save(str(BISMILLAH_TTS_CACHE_PATH))
        dur = get_duration(BISMILLAH_TTS_CACHE_PATH)
        log.info("Synthesized Bismillah TTS audio (%.2fs) -> %s", dur, BISMILLAH_TTS_CACHE_PATH)
        return BISMILLAH_TTS_CACHE_PATH
    except Exception as e:  # noqa: BLE001 - any TTS/network failure should just disable the fallback
        log.warning("Bismillah TTS synthesis failed (%s) — continuing without an intro this run.", e)
        BISMILLAH_TTS_CACHE_PATH.unlink(missing_ok=True)
        return None


# ══════════════════════════════════════════════════════════════════════════
# VISUAL: BLACK -> SPARSE STARS -> SOFT LIGHT
# ══════════════════════════════════════════════════════════════════════════
# Generation grid for the star field: deliberately small (a 10x
# downscale of the output canvas) so a per-pixel probability check
# yields a genuinely sparse scattering of points rather than a dense
# field, then blown back up to full canvas size with a soft blur so
# each point reads as a small glowing dot rather than a hard pixel or a
# "static noise" texture. This is a real point-based star field, not
# film grain — see the calibration note in _build_star_field().
_STAR_GRID_W = max(VIDEO_WIDTH // 10, 20)
_STAR_GRID_H = max(VIDEO_HEIGHT // 10, 20)


def _build_star_field(tmpdir: Path, probability: float) -> Path:
    """
    Generates one small, sparse star-field PNG (RGBA) on a low-res grid
    using ffmpeg's geq `random(idx)` function directly as a per-pixel
    PROBABILITY test (random(1) compared against a threshold in [0,1)),
    rather than the more error-prone approach of multiplying a random
    draw by 255 and thresholding the result — that path went through an
    implicit YUV limited-range conversion that made the true on-pixel
    probability much higher than intended (which is what made the first
    attempt at this read as dense static instead of sparse stars).
    random(1) and random(2) are independent per-pixel random fields
    (each geq function-call register advances its own state every pixel
    evaluated), so random(1) decides WHICH pixels are stars and
    random(2) — passed through pow(x, 2.0) to bias toward dim values —
    decides HOW BRIGHT each star is, giving "mostly dim, only a small
    percentage bright" (per the brief) instead of uniform brightness.

    Calibration actually verified: on a 108x192 grid, probability=0.0018
    was rendered and visually inspected (not just computed) to confirm
    a sparse, natural-looking field with no repeating pattern and no
    static/noise appearance — see the delivery notes for this pass.
    """
    threshold = 1.0 - probability
    star_expr = f"if(gt(random(1),{threshold:.6f}),120+pow(random(2),2.0)*135,0)"

    raw_path = tmpdir / "stars_raw.png"
    star_path = tmpdir / "stars.png"

    cmd_noise = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"nullsrc=s={_STAR_GRID_W}x{_STAR_GRID_H}",
        "-vf", f"geq=r='{star_expr}':g='{star_expr}':b='{star_expr}'",
        "-frames:v", "1", str(raw_path),
    ]
    result = subprocess.run(cmd_noise, capture_output=True, text=True)
    if result.returncode != 0:
        raise IntroBuildError(f"Star field generation failed: {result.stderr[-400:]}")

    # Upscale with the default (bicubic) scaler — NOT nearest-neighbor —
    # so each single low-res star pixel naturally softens into a small
    # glowing point on the full canvas; gblur adds a touch more softness
    # on top so points never look like hard squares.
    cmd_upscale = [
        "ffmpeg", "-y", "-i", str(raw_path),
        "-vf", f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT},gblur=sigma=0.6,format=rgba",
        str(star_path),
    ]
    result = subprocess.run(cmd_upscale, capture_output=True, text=True)
    if result.returncode != 0:
        raise IntroBuildError(f"Star field upscale failed: {result.stderr[-400:]}")
    return star_path


def _build_visual(duration: float, tmpdir: Path, out_path: Path, rng: random.Random) -> None:
    """
    Composes: pure black base -> sparse stars fading in gradually over
    most of the intro -> a very slight overall brightness lift near the
    end ("soft light emerging from darkness," item 1) -> a quick
    fade-in from black at the very start for a clean cinematic open.

    Star opacity and the brightness-lift amount are both jittered
    per-render within configured ranges (item 8: "vary star intensity,
    atmospheric light... while preserving the recognizable structure") —
    the star POSITIONS are freshly randomized every render too (a new
    field is generated each call), so no two intros show the exact same
    field, but the black -> stars -> light -> Bismillah structure never
    changes.
    """
    probability = max(
        0.0002,
        INTRO_STAR_PROBABILITY + rng.uniform(-INTRO_STAR_PROBABILITY_JITTER, INTRO_STAR_PROBABILITY_JITTER),
    )
    opacity = rng.uniform(*INTRO_STAR_OPACITY_RANGE)
    lift_amount = rng.uniform(*INTRO_LIGHT_LIFT_RANGE)

    star_path = _build_star_field(tmpdir, probability)

    fade_span = max(duration * 0.75, 0.15)
    open_fade = min(0.12, duration * 0.25)
    lift_start = duration * 0.6
    lift_span = max(duration - lift_start, 0.05)

    filter_complex = (
        f"[1:v]format=rgba,colorchannelmixer=aa={opacity:.3f},"
        f"fade=t=in:st=0:d={fade_span:.3f}:alpha=1[stars];"
        f"[0:v][stars]overlay=(W-w)/2:(H-h)/2:format=auto[composited];"
        f"[composited]eq=eval=frame:brightness='if(gte(t,{lift_start:.3f}),"
        f"min((t-{lift_start:.3f})/{lift_span:.3f}*{lift_amount:.3f},{lift_amount:.3f}),0)',"
        f"fade=t=in:st=0:d={open_fade:.3f}:color=black[vout]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:d={duration:.3f}:r={INTRO_VISUAL_FPS}",
        "-loop", "1", "-framerate", str(INTRO_VISUAL_FPS), "-i", str(star_path),
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-t", f"{duration:.3f}",
        "-r", str(INTRO_VISUAL_FPS),
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", FINAL_ENCODE_PRESET, "-crf", str(FINAL_ENCODE_CRF),
        "-an",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise IntroBuildError(f"Intro visual composition failed: {result.stderr[-500:]}")


# ══════════════════════════════════════════════════════════════════════════
# AUDIO PREP
# ══════════════════════════════════════════════════════════════════════════

def _prepare_intro_audio(src: Path, duration: float, out_path: Path) -> None:
    """
    Delays the Bismillah audio by INTRO_PRE_BLACK seconds (item 3:
    "0.0-0.25 sec: black" before anything is heard), pads with silence
    up to `duration` if needed (never the reverse — the phrase itself is
    never trimmed), applies a short fade-out so the join into the main
    recitation isn't an abrupt cut, and gently loudness-normalizes it to
    the same target used for the main recitation (item 18: "should not
    overpower the actual Quran recitation" — kept at the same calm
    level rather than louder, even though the two never play
    simultaneously).
    """
    fade_start = max(duration - INTRO_AUDIO_FADE_OUT, 0.0)
    pre_black_ms = int(INTRO_PRE_BLACK * 1000)
    af = (
        f"adelay={pre_black_ms}:all=1,apad,atrim=0:{duration:.3f},"
        f"afade=t=out:st={fade_start:.3f}:d={INTRO_AUDIO_FADE_OUT:.3f},"
        f"loudnorm=I={AUDIO_TARGET_LUFS}:TP=-1.5:LRA=11"
    )
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-af", af,
        "-t", f"{duration:.3f}",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", str(AUDIO_SAMPLE_RATE),
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise IntroBuildError(f"Intro audio prep failed: {result.stderr[-400:]}")


# ══════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINTS
# ══════════════════════════════════════════════════════════════════════════

def build_intro(tmpdir: Path, out_path: Path) -> dict | None:
    """
    Builds the full intro clip (visual + audio, merged, matching the
    main video's canvas/fps) at `out_path`. Returns
    {"path": out_path, "duration": float, "audio_source": str} on
    success, or None if there's no usable Bismillah audio this run
    (see resolve_bismillah_audio() — this is an expected, non-fatal
    outcome, not an error).

    Duration model (item 3, "strict intro timing"): total duration =
    INTRO_PRE_BLACK (silent black beat) + the FULL, uncut Bismillah
    audio + a small tail pad for the fade-out, floored at
    INTRO_MIN_DURATION. This is intentionally NOT stretched or padded
    up to a fixed target the way the previous pass did — the target is
    now "as short as the actual audio allows," not "as long as
    INTRO_MAX_DURATION allows." If the audio itself is long enough that
    this pushes past INTRO_HARD_DURATION_CEILING, that's logged clearly
    (recommending a shorter recording) but the phrase is still never
    cut short.
    """
    audio_src = resolve_bismillah_audio()
    if audio_src is None:
        return None

    raw_dur = get_duration(audio_src)
    tail_pad = 0.10
    duration = max(INTRO_MIN_DURATION, INTRO_PRE_BLACK + raw_dur + tail_pad)

    if duration > INTRO_TARGET_MAX_DURATION:
        level = log.warning if duration > INTRO_HARD_DURATION_CEILING else log.info
        level(
            "Intro will run %.2fs (Bismillah audio is %.2fs) — above the %.2fs target. "
            "The phrase is kept intact rather than cut short; for the snappiest opening, "
            "use a Bismillah recording close to %.1fs.",
            duration, raw_dur, INTRO_TARGET_MAX_DURATION,
            INTRO_TARGET_MAX_DURATION - INTRO_PRE_BLACK - tail_pad,
        )

    rng = random.Random()  # fresh, unseeded — deliberately different every render (item 8)
    visual_path = tmpdir / "intro_visual.mp4"
    audio_path = tmpdir / "intro_audio.m4a"
    _build_visual(duration, tmpdir, visual_path, rng)
    _prepare_intro_audio(audio_src, duration, audio_path)

    cmd = [
        "ffmpeg", "-y", "-i", str(visual_path), "-i", str(audio_path),
        "-map", "0:v", "-map", "1:a",
        # Both streams are already exactly the target codec/bitrate at
        # this point (_build_visual wrote H.264/yuv420p, _prepare_intro_
        # audio wrote AAC at AUDIO_BITRATE) — this step only muxes them
        # into one container, so `-c copy` avoids a redundant re-encode
        # of a stream that's already correct (item 5 of the 2K pass).
        "-c:v", "copy", "-c:a", "copy",
        "-shortest",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise IntroBuildError(f"Intro mux failed: {result.stderr[-400:]}")

    log.info("Intro built: %.2fs (pre-black=%.2fs + audio=%.2fs + pad) -> %s",
              duration, INTRO_PRE_BLACK, raw_dur, out_path.name)
    return {"path": out_path, "duration": round(duration, 2), "audio_source": str(audio_src)}


def join_intro_and_main(intro_path: Path, intro_duration: float, main_path: Path, out_path: Path) -> None:
    """
    Joins the intro onto the main (background + subtitles + recitation
    audio) segment with a short cinematic crossfade (item 4: "the
    entire video should feel like one continuous experience") instead
    of a hard cut. This purely blends already-rendered pixels/audio at
    the seam — it happens AFTER subtitles are burned into `main_path`,
    so it cannot shift subtitle timing in any way.

    This produces the actual final delivered video whenever an intro is
    used, so — like merge_main_segment in build_video.py — it always
    uses FINAL_ENCODE_CRF/PRESET, not a faster/lossier setting (item 5
    of the 2K pass).
    """
    transition = min(INTRO_JOIN_TRANSITION, max(intro_duration - 0.05, 0.05))
    offset = max(intro_duration - transition, 0.0)

    filter_complex = (
        f"[0:v][1:v]xfade=transition=fade:duration={transition:.3f}:offset={offset:.3f}[vout];"
        f"[0:a][1:a]acrossfade=d={transition:.3f}[aout]"
    )
    cmd = [
        "ffmpeg", "-y", "-i", str(intro_path), "-i", str(main_path),
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", FINAL_ENCODE_PRESET, "-crf", str(FINAL_ENCODE_CRF),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", str(AUDIO_SAMPLE_RATE),
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise IntroBuildError(f"Intro/main join failed: {result.stderr[-500:]}")
    log.info("Intro joined onto main video -> %s", out_path.name)
