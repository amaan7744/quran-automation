#!/usr/bin/env python3
"""
video_effects.py

Subtle cinematic motion/effects for Quran background footage.

IMPORTANT:
- motion_filter_fragment() returns an FFmpeg -vf fragment.
- The main pipeline applies this fragment during its existing
  trim/normalize/color-grade pass.
- zoompan is deliberately used with d=1 so every input frame is
  processed instead of repeating the first frame for the duration.
- Subtitle generation and Quran content are completely independent
  of this module.
"""

import random
import subprocess
from pathlib import Path

from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, TRANSITION_DURATION
from visual_themes import MOTION_STYLES
from logging_utils import get_logger

log = get_logger(__name__)

# Extra image area prevents edges appearing during subtle movement.
MOTION_PRE_SCALE = 1.18


def motion_filter_fragment(
    style: str,
    duration: float,
    fps: int = VIDEO_FPS,
    width: int = VIDEO_WIDTH,
    height: int = VIDEO_HEIGHT,
) -> str:
    """
    Return an FFmpeg video-filter fragment that converts a source clip
    into the target canvas with subtle cinematic motion.

    Critical implementation detail:
        zoompan d=1

    FFmpeg's zoompan `d` means the number of output frames generated
    from EACH input frame. Using duration*fps here would repeat one
    source frame for the entire clip and can create long frozen/black
    sections when the first source frame is dark.

    With d=1, input frames continue flowing through the filter normally.
    """

    if duration <= 0:
        raise ValueError(f"duration must be positive, got {duration}")

    if fps <= 0:
        raise ValueError(f"fps must be positive, got {fps}")

    pre_w = int(width * MOTION_PRE_SCALE)
    pre_h = int(height * MOTION_PRE_SCALE)

    # Normalize the source before motion is applied.
    pre_scale = (
        f"scale={pre_w}:{pre_h}:"
        f"force_original_aspect_ratio=increase,"
        f"crop={pre_w}:{pre_h}"
    )

    # Genuine static cinematic shot.
    if style == "static":
        return (
            f"scale={width}:{height}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )

    # ---------------------------------------------------------
    # Zoom-based motion
    # ---------------------------------------------------------

    if style == "push_in":
        # Slowly increase zoom while keeping the visual centered.
        zoom = "min(max(zoom,1.0)+0.0010,1.12)"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"

    elif style == "pull_out":
        # Start slightly zoomed and gently return toward 1.0.
        zoom = "if(eq(on,1),1.12,max(zoom-0.0010,1.0))"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"

    elif style == "drift_horizontal":
        zoom = "1.10"

        # Deterministic movement across the available safe area.
        x = (
            "iw/2-(iw/zoom/2)"
            "+(iw-iw/zoom)*0.20*sin(2*PI*on/"
            f"{max(int(duration * fps), 2)})"
        )
        y = "ih/2-(ih/zoom/2)"

    elif style == "drift_vertical":
        zoom = "1.10"

        x = "iw/2-(iw/zoom/2)"
        y = (
            "ih/2-(ih/zoom/2)"
            "+(ih-ih/zoom)*0.20*sin(2*PI*on/"
            f"{max(int(duration * fps), 2)})"
        )

    elif style == "drift_diagonal":
        zoom = "min(max(zoom,1.0)+0.0006,1.14)"

        x = (
            "iw/2-(iw/zoom/2)"
            "+(iw-iw/zoom)*0.12*sin(2*PI*on/"
            f"{max(int(duration * fps), 2)})"
        )
        y = (
            "ih/2-(ih/zoom/2)"
            "+(ih-ih/zoom)*0.12*sin(2*PI*on/"
            f"{max(int(duration * fps), 2)}+PI/3)"
        )

    elif style == "subtle_rotate":
        # Rotation uses the pre-scaled frame so corners do not expose
        # hard edges. The amplitude is intentionally tiny.
        max_deg = 0.6

        angle = (
            f"{max_deg}*PI/180*"
            f"sin(2*PI*t/{max(duration, 1.0):.3f})"
        )

        return (
            f"{pre_scale},"
            f"rotate=a='{angle}':"
            f"c=black:"
            f"ow={width}:"
            f"oh={height}"
        )

    else:
        log.warning(
            "Unknown motion style %r; falling back to static.",
            style,
        )

        return (
            f"scale={width}:{height}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )

    # ---------------------------------------------------------
    # CRITICAL:
    #
    # d=1 means ONE output frame per input frame.
    #
    # Do NOT use:
    #     d=duration*fps
    #
    # because that repeats each input frame and can freeze the
    # first frame for the entire clip.
    # ---------------------------------------------------------

    return (
        f"{pre_scale},"
        f"zoompan="
        f"z='{zoom}':"
        f"x='{x}':"
        f"y='{y}':"
        f"d=1:"
        f"s={width}x{height}:"
        f"fps={fps}"
    )


def pick_motion_style(
    motion_pool: list = None,
    rng: random.Random = None,
) -> str:
    """
    Select one motion style from the configured pool.
    """
    rng = rng or random
    pool = motion_pool or MOTION_STYLES

    if not pool:
        return "static"

    return rng.choice(pool)


def apply_motion(
    src: Path,
    dst: Path,
    duration: float,
    fps: int = VIDEO_FPS,
    style: str = None,
) -> str:
    """
    Standalone convenience wrapper for testing/manual use.

    The main pipeline normally applies motion directly through
    pexels_fetcher.trim_and_normalize().
    """

    if not src.exists():
        raise FileNotFoundError(f"Motion source does not exist: {src}")

    if duration <= 0:
        raise ValueError(f"duration must be positive, got {duration}")

    style = style or pick_motion_style()

    vf = (
        f"{motion_filter_fragment(style, duration, fps)},"
        "format=yuv420p"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-t",
        f"{duration:.3f}",
        "-vf",
        vf,
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "cfr",
        "-r",
        str(fps),
        str(dst),
    ]

    log.info(
        "Applying %s motion: %s -> %s",
        style,
        src.name,
        dst.name,
    )

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Motion pass failed on {src.name}: "
            f"{result.stderr[-1000:]}"
        )

    return style


def atmosphere_overlay_fragment(intensity: str) -> str:
    """
    Return a very subtle atmospheric grain fragment.

    This is intentionally restrained. The visual references favor
    dark, comfortable, cinematic footage rather than obvious filters.
    """

    amount = {
        "low": 3,
        "medium": 6,
        "high": 9,
    }.get(intensity, 0)

    if amount <= 0:
        return ""

    return f"noise=alls={amount}:allf=t+u"


def crossfade_concat(
    clip_paths: list,
    durations: list,
    out_path: Path,
    transition_style: str = "fade",
    transition: float = TRANSITION_DURATION,
    fps: int = VIDEO_FPS,
) -> None:
    """
    Standalone FFmpeg xfade helper.

    The main production pipeline uses the implementation in
    pexels_fetcher.py, which owns the normalized timebase.
    """

    if not clip_paths:
        raise ValueError("No clips supplied for crossfade_concat.")

    if len(clip_paths) != len(durations):
        raise ValueError(
            "clip_paths and durations must have the same length."
        )

    if len(clip_paths) == 1:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(clip_paths[0]),
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            str(out_path),
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"Single-clip render failed: {result.stderr[-500:]}"
            )

        return

    inputs = []

    for path in clip_paths:
        inputs.extend(["-i", str(path)])

    filter_parts = []

    cumulative = 0.0
    last_label = "0:v"

    for i in range(1, len(clip_paths)):
        offset = max(
            cumulative + durations[i - 1] - transition,
            0.1,
        )

        out_label = (
            f"v{i}"
            if i < len(clip_paths) - 1
            else "vout"
        )

        filter_parts.append(
            f"[{last_label}][{i}:v]"
            f"xfade="
            f"transition={transition_style}:"
            f"duration={transition}:"
            f"offset={offset:.3f}"
            f"[{out_label}]"
        )

        cumulative += durations[i - 1] - transition
        last_label = out_label

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg",
        "-y",
        *inputs,
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-an",
        str(out_path),
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Crossfade concat failed: {result.stderr[-1000:]}"
        )

    log.info(
        "Crossfade background assembled -> %s",
        out_path.name,
    )
