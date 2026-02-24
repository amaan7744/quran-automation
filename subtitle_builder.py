#!/usr/bin/env python3
"""
subtitle_builder.py
Builds an ASS subtitle file with two clearly visible tracks:
  - Arabic  : top-center, large, bold, white with strong black outline
  - English : bottom-center, medium, white with strong black outline
Each ayah shown for exactly its audio duration.
Fonts scaled for 2K (1440x2560) so text is always clearly visible.
"""

import textwrap
from pathlib import Path


def get_ayah_text(json_data, surah: int, ayah: int) -> str:
    """
    Extract text from JSON. Supports 3 formats:
    A. Flat list : [{"surah": 1, "ayah": 1, "text": "..."}]
    B. Nested    : {"1": {"1": "text"}}
    C. verse_key : [{"verse_key": "1:1", "text": "..."}]
    """
    if isinstance(json_data, list):
        for item in json_data:
            if item.get("surah") == surah and item.get("ayah") == ayah:
                return item.get("text", "")
            if item.get("verse_key") == f"{surah}:{ayah}":
                return item.get("text", "")
    elif isinstance(json_data, dict):
        return json_data.get(str(surah), {}).get(str(ayah), "")
    return ""


def sec_to_ass(s: float) -> str:
    h   = int(s // 3600)
    m   = int((s % 3600) // 60)
    sec = s % 60
    return f"{h}:{m:02d}:{sec:05.2f}"


def build_subtitles(
    batch:           list,
    arabic_data,
    english_data,
    audio_durations: list,
    out_path:        Path,
) -> None:
    """
    Write ASS subtitle file.
    Arabic  -> upper area, very large, bold, strong outline = always visible
    English -> lower area, large, strong outline = always visible
    Both have semi-transparent dark background box for maximum readability.
    """

    # PlayRes matches our 2K output (1440x2560)
    # BorderStyle 3 = opaque box behind text for guaranteed visibility
    # Outline 5 = thick black outline so text visible on any background
    # Shadow 3 = drop shadow for extra depth
    # MarginV = vertical margin from edge
    # Alignment 8 = top-center for Arabic, 2 = bottom-center for English
    header = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1440
PlayResY: 2560
ScaledBorderAndShadow: yes
WrapStyle: 1

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Arabic,Noto Naskh Arabic,110,&H00FFFFFF,&H000000FF,&H00000000,&H99000000,1,0,0,0,100,100,3,0,3,5,3,8,80,80,160,1
Style: English,Noto Sans,62,&H00FFFFFF,&H000000FF,&H00000000,&H99000000,0,0,0,0,100,100,0,0,3,4,2,2,80,80,120,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events = []
    cursor = 0.0

    for (surah, ayah), duration in zip(batch, audio_durations):
        start = sec_to_ass(cursor)
        end   = sec_to_ass(cursor + duration)

        ar_text = get_ayah_text(arabic_data, surah, ayah)
        en_text = get_ayah_text(english_data, surah, ayah)

        # English: wrap at 36 chars for clean 2-line display on mobile
        en_wrapped = r"\N".join(textwrap.wrap(en_text, width=36)) if en_text else ""

        if ar_text:
            events.append(f"Dialogue: 0,{start},{end},Arabic,,0,0,0,,{ar_text}")
        if en_wrapped:
            events.append(f"Dialogue: 0,{start},{end},English,,0,0,0,,{en_wrapped}")

        cursor += duration

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
        f.write("\n")

    print(f"  Subtitles written -> {out_path.name} ({len(events)} lines)")
