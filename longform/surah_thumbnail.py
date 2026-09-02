#!/usr/bin/env python3
"""
surah_thumbnail.py
Generates a 1280x720 YouTube thumbnail for a Surah: a still frame pulled
from the middle of that video's own cinematic background (so the
thumbnail always matches the actual footage, never a generic stock
image), darkened slightly for text contrast, with the Arabic + English
Surah name burned in via the same libass "ass" filter used everywhere
else in this pipeline (correct Arabic shaping, no drawtext).
"""

import subprocess
from pathlib import Path

from subtitle_builder import escape_ass
from logging_utils import get_logger

log = get_logger(__name__)

THUMB_WIDTH = 1280
THUMB_HEIGHT = 720

_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: NameAr,{ar_font},96,&H00F5F5F5,&H00F5F5F5,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,4,0,2,60,60,300,1
Style: NameEn,{en_font},58,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,3,0,1,3,0,2,60,60,180,1
Style: Sub,{en_font},36,&H00D8D8D8,&H00D8D8D8,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,60,60,90,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_thumbnail(background_video: Path, surah_info: dict, out_path: Path,
                     arabic_font: str, english_font: str, timestamp: float = None) -> None:
    """
    `timestamp` (seconds into background_video) defaults to its midpoint,
    picked once ffprobe reports the actual duration.
    """
    if timestamp is None:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(background_video)],
            capture_output=True, text=True,
        )
        try:
            duration = float(r.stdout.strip())
        except ValueError:
            duration = 10.0
        timestamp = duration / 2

    ass_path = out_path.with_suffix(".ass")
    header = _HEADER.format(w=THUMB_WIDTH, h=THUMB_HEIGHT, ar_font=arabic_font, en_font=english_font)
    events = [
        f"Dialogue: 0,0:00:00.00,0:00:05.00,NameAr,,0,0,0,,{escape_ass(surah_info['name_ar'])}",
        f"Dialogue: 0,0:00:00.00,0:00:05.00,NameEn,,0,0,0,,SURAH {escape_ass(surah_info['name_en'].upper())}",
        f"Dialogue: 0,0:00:00.00,0:00:05.00,Sub,,0,0,0,,Complete Quran Recitation",
    ]
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
        f.write("\n")

    vf = (
        f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={THUMB_WIDTH}:{THUMB_HEIGHT},"
        # Gentle darken + a bottom-weighted gradient (via vignette) so
        # burned-in text stays readable over any footage, without
        # flattening the image into a plain dark rectangle.
        f"eq=brightness=-0.06:contrast=1.05,"
        f"ass={ass_path}"
    )
    cmd = [
        "ffmpeg", "-y", "-ss", str(timestamp), "-i", str(background_video),
        "-frames:v", "1", "-vf", vf, "-q:v", "2",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Thumbnail generation failed: {result.stderr[-500:]}")
    log.info("Thumbnail generated -> %s", out_path.name)
