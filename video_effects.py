#!/usr/bin/env python3
"""
video_effects.py
Turns a set of static stock clips into a professionally-edited moving
background: slow zoom, pan/drift, and crossfade transitions between
cuts — never a hard-cut slideshow.
"""

import random
import subprocess
from pathlib import Path

from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, TRANSITION_DURATION, MOTION_STYLES
from logging_utils import get_logger

log = get_logger(__name__)


def _zoompan_expr(style: str, duration: float, fps: int) -> str:
    """
    Build a zoompan filter expression for the given motion style.
    zoompan operates on a per-frame zoom/x/y expression, so we scale the
    source up first (in the caller) to give it room to pan without
    exposing edges.
    """
    frames = max(int(duration * fps), 2)

    if style == "zoom_in":
        z = f"min(zoom+0.0012,1.25)"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif style == "zoom_out":
        z = f"if(eq(on,0),1.25,max(zoom-0.0012,1.0))"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif style == "pan_left":
        z = "1.15"
        x, y = "if(eq(on,0),iw*0.15,x-1.2)", "ih/2-(ih/zoom/2)"
    elif style == "pan_right":
        z = "1.15"
        x, y = "if(eq(on,0),0,x+1.2)", "ih/2-(ih/zoom/2)"
    elif style == "pan_up":
        z = "1.15"
        x, y = "iw/2-(iw/zoom/2)", "if(eq(on,0),ih*0.15,y-1.0)"
    else:  # "drift" — slow diagonal drift, most subtle/cinematic
        z = f"min(zoom+0.0008,1.15)"
        x, y = "if(eq(on,0),iw*0.1,x+0.4)", "if(eq(on,0),ih*0.1,y+0.3)"

    return f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={fps}"


def apply_motion(src: Path, dst: Path, duration: float, fps: int = VIDEO_FPS, style: str = None) -> str:
    """
    Normalizes a clip to the output canvas and applies a subtle Ken-Burns
    style motion effect. Returns the style used (for logging/variety tracking).
    """
    style = style or random.choice(MOTION_STYLES)

    # Upscale ~20% beyond target so zoompan/pan never exposes a hard edge.
    pre_scale = (
        f"scale={int(VIDEO_WIDTH*1.3)}:{int(VIDEO_HEIGHT*1.3)}:force_original_aspect_ratio=increase,"
        f"crop={int(VIDEO_WIDTH*1.3)}:{int(VIDEO_HEIGHT*1.3)}"
    )
    motion = _zoompan_expr(style, duration, fps)
    vf = f"{pre_scale},{motion},format=yuv420p"

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


def crossfade_concat(clip_paths: list, durations: list, out_path: Path,
                      transition: float = TRANSITION_DURATION, fps: int = VIDEO_FPS) -> None:
    """
    Joins normalized clips with xfade crossfade transitions (with a subtle
    blur mixed in) instead of hard cuts, producing one continuous background.
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
        offset = cumulative + durations[i - 1] - transition
        offset = max(offset, 0.1)
        out_label = f"v{i}" if i < len(clip_paths) - 1 else "vout"
        # alternate xfade transition styles for visual variety
        xfade_style = random.choice(["fade", "fadeblack", "wipeleft", "smoothleft", "dissolve"])
        filter_parts.append(
            f"[{last_label}][{i}:v]xfade=transition={xfade_style}:duration={transition}:offset={offset:.3f}[{out_label}]"
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
