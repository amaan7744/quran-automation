#!/usr/bin/env python3
"""
subtitle_builder.py
Pipeline Step 2 — Builds Arabic + English ASS subtitle file.

- Reads batch_info.json (written by audio_downloader.py)
- Reads arabic.json and english.json
- Writes subtitles.ass with timed lines per ayah
"""

import json
import textwrap
from pathlib import Path

ARABIC_JSON     = Path("arabic.json")
ENGLISH_JSON    = Path("english.json")
BATCH_INFO_FILE = Path("batch_info.json")
SUBTITLE_FILE   = Path("subtitles.ass")

# 2K 9:16 resolution
PLAY_RES_X = 1440
PLAY_RES_Y = 2560


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_ayah_text(data, surah: int, ayah: int) -> str:
    """
    Supports 3 JSON formats:
    A. Flat list:  [{"surah": 1, "ayah": 1, "text": "..."}]
    B. verse_key:  [{"verse_key": "1:1", "text": "..."}]
    C. Nested:     {"1": {"1": "text"}}
    """
    if isinstance(data, list):
        for item in data:
            if item.get("surah") == surah and item.get("ayah") == ayah:
                return item.get("text", "")
            if item.get("verse_key") == f"{surah}:{ayah}":
                return item.get("text", "")
    elif isinstance(data, dict):
        return data.get(str(surah), {}).get(str(ayah), "")
    return ""


def sec_to_ass(s: float) -> str:
    h   = int(s // 3600)
    m   = int((s % 3600) // 60)
    sec = s % 60
    return f"{h}:{m:02d}:{sec:05.2f}"


def build_ass(batch, audio_durations, arabic_data, english_data, out: Path) -> None:
    header = f"""\
[Script Info]
ScriptType: v4.00+
PlayResX: {PLAY_RES_X}
PlayResY: {PLAY_RES_Y}
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Arabic,Noto Naskh Arabic,105,&H00FFFFFF,&H000000FF,&H00000000,&HAA000000,1,0,0,0,100,100,2,0,1,5,3,8,80,80,120,1
Style: English,Noto Sans,58,&H00FFFAF0,&H000000FF,&H00000000,&HAA000000,0,0,0,0,100,100,0,0,1,4,2,2,80,80,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    cursor = 0.0

    for (surah, ayah), dur in zip(batch, audio_durations):
        start = sec_to_ass(cursor)
        end   = sec_to_ass(cursor + dur)

        ar = get_ayah_text(arabic_data, surah, ayah)
        en = get_ayah_text(english_data, surah, ayah)
        en_wrapped = r"\N".join(textwrap.wrap(en, width=38)) if en else ""

        if ar:
            events.append(f"Dialogue: 0,{start},{end},Arabic,,0,0,0,,{ar}")
        if en_wrapped:
            events.append(f"Dialogue: 0,{start},{end},English,,0,0,0,,{en_wrapped}")

        cursor += dur

    with open(out, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
        f.write("\n")


def main():
    print("=" * 40)
    print("STEP 2 — SUBTITLE BUILDER")
    print("=" * 40)

    print("\nLoading batch info...")
    info = load_json(BATCH_INFO_FILE)
    batch           = [tuple(x) for x in info["batch"]]
    audio_durations = info["audio_durations"]
    surah_name_en   = info["surah_name_en"]
    surah_num       = info["surah_num"]
    print(f"  Surah {surah_num} ({surah_name_en}), {len(batch)} ayahs")

    print("\nLoading Quran text files...")
    arabic_data  = load_json(ARABIC_JSON)
    english_data = load_json(ENGLISH_JSON)

    print("\nBuilding ASS subtitle file...")
    build_ass(batch, audio_durations, arabic_data, english_data, SUBTITLE_FILE)
    print(f"  Done -> {SUBTITLE_FILE}")
    print(f"  Subtitles: {len(batch) * 2} dialogue lines (Arabic + English per ayah)")


if __name__ == "__main__":
    main()
