#!/usr/bin/env python3
"""
video_effects.py
Turns a static stock clip into a professionally-edited moving
background: a controlled variety of subtle per-clip motion styles
(push-in, pull-out, drift, static, subtle rotation — item 8 of the
brief) instead of every clip using the same zoom.

Design note: motion_filter_fragment() below returns a pure ffmpeg -vf
filter FRAGMENT (a string), not a rendered file. pexels_fetcher.py
stitches this fragment directly into its own single trim/normalize/
color-grade ffmpeg pass so a clip is only ever re-encoded once. This
module also exposes apply_motion(), a standalone convenience wrapper
that runs the fragment as its own ffmpeg pass — kept for direct/manual
use and for spot-testing — but the pipeline's hot path
(pexels_fetcher.trim_and_normalize) does not call it, to avoid a second
unnecessary re-encode per clip.
"""

import random
import subprocess
from pathlib import Path

from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, TRANSITION_DURATION
from visual_themes import MOTION_STYLES
from logging_utils import get_logger

log = get_logger(__name__)

# How much headroom (as a fraction) a clip is pre-scaled beyond the
# output canvas before a pan/drift/rotate is applied, so motion never
# exposes a hard edge. Zoom-based styles (push_in/pull_out) already
# reach further than this on their own; drift/rotate need this extra
# margin since they don't zoom much.
MOTION_PRE_SCALE = 1.18


def motion_filter_fragment(style: str, duration: float, fps: int = VIDEO_FPS,
                            width: int = VIDEO_WIDTH, height: int = VIDEO_HEIGHT) -> str:
    """
    Returns an ffmpeg -vf FRAGMENT (no leading/trailing comma) that pre-
    scales the frame with headroom and applies one subtle motion style,
    landing on exactly `width`x`height`. Caller is expected to have
    already normalized fps/SAR/pixel format around this fragment (see
    pexels_fetcher.trim_and_normalize).

    All motion is intentionally slow and small — the Quran text stays
    the primary visual focus (item 8: "Keep everything subtle").
    """
    frames = max(int(duration * fps), 2)
    pre_w, pre_h = int(width * MOTION_PRE_SCALE), int(height * MOTION_PRE_SCALE)
    pre_scale = (
        f"scale={pre_w}:{pre_h}:force_original_aspect_ratio=increase,"
        f"crop={pre_w}:{pre_h}"
    )

    if style == "static":
        # A genuine no-motion cinematic shot (item 8) — still normalized
        # to the output canvas, just without zoompan at all.
        return f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"

    if style == "push_in":
        z = "min(zoom+0.0010,1.12)"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif style == "pull_out":
        z = "if(eq(on,0),1.12,max(zoom-0.0010,1.0))"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif style == "drift_horizontal":
        z = "1.10"
        x = "if(eq(on,0),iw*0.12,x+0.35)"
        y = "ih/2-(ih/zoom/2)"
    elif style == "drift_vertical":
        z = "1.10"
        x = "iw/2-(iw/zoom/2)"
        y = "if(eq(on,0),ih*0.12,y+0.30)"
    elif style == "drift_diagonal":
        z = "min(zoom+0.0006,1.14)"
        x = "if(eq(on,0),iw*0.08,x+0.30)"
        y = "if(eq(on,0),ih*0.08,y+0.22)"
    elif style == "subtle_rotate":
        # zoompan has no rotation param, so this style uses `rotate`
        # instead of zoompan — an extremely small oscillation
        # (~0.6 degrees peak), imperceptible as "spinning," just enough
        # to feel alive. Falls through to a different filter chain.
        max_deg = 0.6
        angle = f"{max_deg}*PI/180*sin(2*PI*t/{max(duration, 1.0):.2f})"
        return (
            f"{pre_scale},"
            f"rotate=a='{angle}':c=black:ow={width}:oh={height}"
        )
    else:
        # Unknown style name — degrade gracefully to a static shot
        # rather than raising, since motion is a polish layer, never a
        # reason to fail the whole render.
        log.warning("Unknown motion style %r — falling back to static.", style)
        return f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"

    return (
        f"{pre_scale},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={fps}"
    )


def pick_motion_style(motion_pool: list = None, rng: random.Random = None) -> str:
    rng = rng or random
    pool = motion_pool or MOTION_STYLES
    return rng.choice(pool)


def apply_motion(src: Path, dst: Path, duration: float, fps: int = VIDEO_FPS, style: str = None) -> str:
    """
    Standalone convenience wrapper: normalizes a clip to the output
    canvas and applies one motion style in its own ffmpeg pass. Not used
    by the main pipeline (see module docstring) but kept for direct/
    manual use and spot-testing. Returns the style used.
    """
    style = style or pick_motion_style()
    vf = f"{motion_filter_fragment(style, duration, fps)},format=yuv420p"

    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-t", str(duration),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-an",
        str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Motion pass failed on {src.name}: {result.stderr[-400:]}")
    return style


def atmosphere_overlay_fragment(intensity: str) -> str:
    """
    Returns an optional, very light atmospheric grain/haze fragment (item
    6: "micro-cinematic events") to append after color grading, or ""
    for no overlay. Deliberately subtle — this is felt, not seen; never
    a heavy "film grain" preset. intensity should be one of "low",
    "medium", "high" (see visual_themes.TEMPLATES atmosphere_intensity).
    """
    amount = {"low": 3, "medium": 6, "high": 9}.get(intensity, 0)
    if amount <= 0:
        return ""
    # noise=alls=<amount> at a low level reads as fine atmospheric
    # grain/dust rather than digital noise. allf=t+u varies it over
    # time so it never looks like a fixed overlay baked into the
    # footage.
    return f"noise=alls={amount}:allf=t+u"


def crossfade_concat(clip_paths: list, durations: list, out_path: Path,
                      transition_style: str = "fade",
                      transition: float = TRANSITION_DURATION, fps: int = VIDEO_FPS) -> None:
    """
    Joins normalized clips with a single, consistent xfade transition
    style (chosen once per reel by visual_engine.py so the whole reel
    reads as one intentional edit, not a random transition per cut).
    Not used by the main pipeline — pexels_fetcher.crossfade_concat is
    the one actually wired into build_background(), since it also owns
    the identical-timebase guarantees that xfade depends on. Kept here
    as a lightweight, dependency-free alternative for standalone/manual
    use of this module.
    """
    if len(clip_paths) == 1:
        cmd = ["ffmpeg", "-y", "-i", str(clip_paths[0]), "-c", "copy", str(out_path)]
        subprocess.run(cmd, check=True, capture_output=True)
        return

    inputs = []
    for p in clip_paths:
        inputs += ["-i", str(p)]

    filter_parts = []
    cumulative = 0.0
    last_label = "0:v"

    for i in range(1, len(clip_paths)):
        offset = max(cumulative + durations[i - 1] - transition, 0.1)
        out_label = f"v{i}" if i < len(clip_paths) - 1 else "vout"
        filter_parts.append(
            f"[{last_label}][{i}:v]xfade=transition={transition_style}:duration={transition}:offset={offset:.3f}[{out_label}]"
        )
        cumulative += durations[i - 1] - transition
        last_label = out_label

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-r", str(fps),
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-an",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Crossfade concat failed: {result.stderr[-500:]}")
    log.info("Crossfade background assembled -> %s", out_path.name)
