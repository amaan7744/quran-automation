#!/usr/bin/env python3
"""
subtitle_builder.py
Builds ASS subtitle file from arabic.json and english.json.
Arabic top-center, English bottom-center.
Both always visible with dark background box and thick outline.

Supports ALL known JSON formats including the nested surahs format:
{
  "metadata": {...},
  "surahs": [
    {
      "number": 1,
      "name": "Al-Fatihah",
      "ayahs": [
        {"number": 1, "text": "..."},
        ...
      ]
    }
  ]
}
"""

import textwrap
from pathlib import Path


def get_ayah_text(json_data, surah: int, ayah: int) -> str:
    """
    Extract ayah text. Handles ALL known formats:

    Format A - Nested surahs (YOUR FORMAT):
      {"metadata": {...}, "surahs": [{"number": 1, "ayahs": [{"number": 1, "text": "..."}]}]}

    Format B - Flat list:
      [{"surah": 1, "ayah": 1, "text": "..."}]

    Format C - verse_key list:
      [{"verse_key": "1:1", "text": "..."}]

    Format D - Nested dict:
      {"1": {"1": "text"}}
    """

    # ── Format A: nested surahs (Tanzil / Sahih International format) ─────────
    if isinstance(json_data, dict) and "surahs" in json_data:
        for s in json_data["surahs"]:
            if s.get("number") == surah:
                for a in s.get("ayahs", []):
                    if a.get("number") == ayah:
                        return a.get("text", "").strip()
        return ""

    # ── Format B: flat list with surah/ayah keys ──────────────────────────────
    if isinstance(json_data, list):
        for item in json_data:
            # integer keys
            if item.get("surah") == surah and item.get("ayah") == ayah:
                return item.get("text", "").strip()
            # string keys
            if str(item.get("surah")) == str(surah) and str(item.get("ayah")) == str(ayah):
                return item.get("text", "").strip()
            # Format C: verse_key
            if item.get("verse_key") == f"{surah}:{ayah}":
                return item.get("text", "").strip()
        return ""

    # ── Format D: nested dict {"1": {"1": "text"}} ────────────────────────────
    if isinstance(json_data, dict):
        val = json_data.get(str(surah), {}).get(str(ayah), "")
        return val.strip() if val else ""

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
    Write ASS subtitle file with Arabic and English text.
    Arabic  -> top-center, 110px, bold, dark box = always visible
    English -> bottom-center, 62px, dark box = always visible
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

        # Debug print so logs show what was found
        ar_preview = ar_text[:50] if ar_text else "NOT FOUND"
        en_preview = en_text[:50] if en_text else "NOT FOUND"
        print(f"    {surah}:{ayah} | AR: '{ar_preview}' | EN: '{en_preview}'")

        en_wrapped = r"\N".join(textwrap.wrap(en_text, width=36)) if en_text else ""

        if ar_text:
            events.append(f"Dialogue: 0,{start},{end},Arabic,,0,0,0,,{ar_text}")
        if en_wrapped:
            events.append(f"Dialogue: 0,{start},{end},English,,0,0,0,,{en_wrapped}")

        cursor += duration

    print(f"  Total subtitle events: {len(events)}")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
        f.write("\n")

    print(f"  Subtitle file written -> {out_path.name}")
