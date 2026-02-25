#!/usr/bin/env python3
"""
subtitle_builder.py
Optimized for Transparent, Centered, and Stacked Quranic Subtitles.
"""

import textwrap
from pathlib import Path

def get_ayah_text(json_data, surah: int, ayah: int) -> str:
    """Extract ayah text handling nested surahs format."""
    if isinstance(json_data, dict) and "surahs" in json_data:
        for s in json_data["surahs"]:
            if s.get("number") == surah:
                for a in s.get("ayahs", []):
                    if a.get("number") == ayah:
                        return a.get("text", "").strip()
    return ""

def sec_to_ass(s: float) -> str:
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h}:{m:02d}:{sec:05.2f}"

def build_subtitles(
    batch: list,
    arabic_data,
    english_data,
    audio_durations: list,
    out_path: Path,
) -> None:
    """
    Write ASS subtitle file.
    Arabic: Amiri font, 145px, top-center stack.
    English: Noto Sans, 75px, bottom-center stack.
    Background: Transparent with thick outlines for readability.
    """

    header = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1440
PlayResY: 2560
ScaledBorderAndShadow: yes
WrapStyle: 1

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Arabic,Amiri,145,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,2,0,1,6,0,5,100,100,60,1
Style: English,Noto Sans,75,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,4,0,5,120,120,200,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events = []
    cursor = 0.0

    for (surah, ayah), duration in zip(batch, audio_durations):
        start = sec_to_ass(cursor)
        end = sec_to_ass(cursor + duration)

        ar_text = get_ayah_text(arabic_data, surah, ayah)
        en_text = get_ayah_text(english_data, surah, ayah)

        # Wrap English text to keep the center block clean
        en_wrapped = r"\N".join(textwrap.wrap(en_text, width=32)) if en_text else ""

        if ar_text:
            events.append(f"Dialogue: 0,{start},{end},Arabic,,0,0,0,,{ar_text}")
        if en_wrapped:
            events.append(f"Dialogue: 0,{start},{end},English,,0,0,0,,{en_wrapped}")

        cursor += duration

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
        f.write("\n")

    print(f"Subtitle file written -> {out_path.name}")
