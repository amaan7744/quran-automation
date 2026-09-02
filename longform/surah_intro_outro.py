#!/usr/bin/env python3
"""
surah_intro_outro.py
Generates the intro and outro segments for a long-form Surah video as
short, silent 3840x2160 clips: black background, fade-in/out title card,
rendered through the SAME libass "ass" ffmpeg filter the main subtitle
track uses (see surah_subtitles.py) so Arabic shaping/RTL/diacritics are
correct here too — never ffmpeg drawtext, which does not shape Arabic.

Every piece of text is pulled from the dynamic Surah metadata
(quran_metadata.get_surah_info) — nothing here is hard-coded to any one
Surah (spec sections 13/14/15).
"""

import subprocess
from pathlib import Path

from subtitle_builder import escape_ass, sec_to_ass
from config import (
    LONGFORM_VIDEO_WIDTH, LONGFORM_VIDEO_HEIGHT, LONGFORM_VIDEO_FPS,
    LONGFORM_ARABIC_FONT, LONGFORM_ENGLISH_FONT,
    LONGFORM_INTRO_DURATION, LONGFORM_OUTRO_DURATION, LONGFORM_OUTRO_MESSAGE,
    LONGFORM_RECITER_NAME, LONGFORM_AUDIO_SAMPLE_RATE,
)
from logging_utils import get_logger

log = get_logger(__name__)

PLAY_RES_X = LONGFORM_VIDEO_WIDTH
PLAY_RES_Y = LONGFORM_VIDEO_HEIGHT

_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: {res_x}
PlayResY: {res_y}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Bismillah,{ar_font},96,&H00E6D9B8,&H00E6D9B8,&H000A0A0A,&H00000000,0,0,0,0,100,100,0,0,1,2,1,2,200,200,{y1},1
Style: NameAr,{ar_font},160,&H00F5F5F5,&H00F5F5F5,&H000A0A0A,&H00000000,0,0,0,0,100,100,0,0,1,3,2,2,200,200,{y2},1
Style: NameEn,{en_font},80,&H00E6F4FF,&H00E6F4FF,&H000A0A0A,&H00000000,1,0,0,0,100,100,2,0,1,2,1.5,2,200,200,{y3},1
Style: Sub,{en_font},52,&H00B0B0B0,&H00B0B0B0,&H000A0A0A,&H00000000,0,0,0,0,100,100,0,0,1,1.5,1,2,200,200,{y4},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _render_title_card(events: list, duration: float, out_path: Path,
                        y1: int, y2: int, y3: int, y4: int) -> None:
    ass_path = out_path.with_suffix(".ass")
    header = _HEADER.format(
        res_x=PLAY_RES_X, res_y=PLAY_RES_Y,
        ar_font=LONGFORM_ARABIC_FONT, en_font=LONGFORM_ENGLISH_FONT,
        y1=y1, y2=y2, y3=y3, y4=y4,
    )
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
        f.write("\n")

    # Slow fade in from pure black, hold, slow fade to black — a still
    # black source is intentional (spec section 13/15: elegant, short,
    # not a nature-clip stinger competing with the title text).
    vf = f"ass={ass_path}"
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={PLAY_RES_X}x{PLAY_RES_Y}:r={LONGFORM_VIDEO_FPS}:d={duration}",
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={LONGFORM_AUDIO_SAMPLE_RATE}",
        "-vf", vf,
        "-t", str(duration),
        # Profile/pix_fmt/audio format deliberately match render_main_body's
        # encode exactly (see surah_renderer.py) so the two are eligible
        # for a stream-copy concat — see surah_renderer.join_segments.
        "-c:v", "libx264", "-profile:v", "high", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", str(LONGFORM_AUDIO_SAMPLE_RATE), "-shortest",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Title card render failed ({out_path.name}): {result.stderr[-500:]}")


def build_intro(surah_info: dict, out_path: Path, duration: float = LONGFORM_INTRO_DURATION) -> float:
    """
    Fade in from black -> Bismillah -> Surah name (Arabic + English) ->
    verse count / revelation type -> fades to black just before the
    recitation begins. Returns the actual rendered duration.
    """
    fade = "\\fad(600,500)"
    center_y = PLAY_RES_Y // 2
    y1, y2, y3, y4 = center_y - 420, center_y - 140, center_y + 140, center_y + 300

    bismillah_ar = "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ"
    sub_line = f"{surah_info['ayah_count']} Verses  •  {surah_info['revelation_type']}"

    events = [
        f"Dialogue: 0,{sec_to_ass(0)},{sec_to_ass(duration)},Bismillah,,0,0,0,,"
        f"{{{fade}}}{escape_ass(bismillah_ar)}",
        f"Dialogue: 0,{sec_to_ass(0)},{sec_to_ass(duration)},NameAr,,0,0,0,,"
        f"{{{fade}}}{escape_ass(surah_info['name_ar'])}",
        f"Dialogue: 0,{sec_to_ass(0)},{sec_to_ass(duration)},NameEn,,0,0,0,,"
        f"{{{fade}}}SURAH {escape_ass(surah_info['name_en'].upper())}",
        f"Dialogue: 0,{sec_to_ass(0)},{sec_to_ass(duration)},Sub,,0,0,0,,"
        f"{{{fade}}}{escape_ass(sub_line)}",
    ]
    _render_title_card(events, duration, out_path, y1, y2, y3, y4)
    log.info("Intro rendered -> %s (%.1fs)", out_path.name, duration)
    return duration


def build_outro(surah_info: dict, out_path: Path, duration: float = LONGFORM_OUTRO_DURATION,
                 closing_message: str = LONGFORM_OUTRO_MESSAGE) -> float:
    """Short fade -> Surah name -> optional closing message -> fade to
    black. No music, no abrupt cut (spec section 15)."""
    fade = "\\fad(500,600)"
    center_y = PLAY_RES_Y // 2
    y2, y3, y4 = center_y - 180, center_y + 100, center_y + 260

    events = [
        f"Dialogue: 0,{sec_to_ass(0)},{sec_to_ass(duration)},NameAr,,0,0,0,,"
        f"{{{fade}}}{escape_ass(surah_info['name_ar'])}",
        f"Dialogue: 0,{sec_to_ass(0)},{sec_to_ass(duration)},NameEn,,0,0,0,,"
        f"{{{fade}}}SURAH {escape_ass(surah_info['name_en'].upper())} — COMPLETE",
    ]
    if closing_message:
        events.append(
            f"Dialogue: 0,{sec_to_ass(0)},{sec_to_ass(duration)},Sub,,0,0,0,,"
            f"{{{fade}}}{escape_ass(closing_message)}"
        )
    events.append(
        f"Dialogue: 0,{sec_to_ass(0)},{sec_to_ass(duration)},Sub,,0,0,0,,"
        f"{{{fade}}}\\N\\NRecitation: {escape_ass(LONGFORM_RECITER_NAME)}"
    )
    _render_title_card(events, duration, out_path, y2 - 200, y2, y3, y4)
    log.info("Outro rendered -> %s (%.1fs)", out_path.name, duration)
    return duration
