#!/usr/bin/env python3
"""Live Quran subtitle helpers.

Keeps the subtitle renderer independent from the audio pipeline. FFmpeg reads
these two text files with drawtext/reload=1 while the Python supervisor atomically
replaces them at each ayah boundary.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from live.live_config import (
    ARABIC_DATA_FILE,
    ENGLISH_DATA_FILE,
    ARABIC_FONT_FILE,
    ENGLISH_FONT_FILE,
    SUBTITLE_DIR,
    RECITER_NAME,
)


def _load(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


_AR = _load(ARABIC_DATA_FILE)
_EN = _load(ENGLISH_DATA_FILE)


def _ayah(data: dict, surah: int, ayah: int) -> str:
    for s in data.get("surahs", []):
        if int(s.get("number", -1)) == surah:
            for a in s.get("ayahs", []):
                if int(a.get("number", -1)) == ayah:
                    return str(a.get("text", "")).strip()
    return ""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _wrap_words(text: str, max_chars: int) -> str:
    """Wrap subtitle text to at most two readable lines."""
    words = text.split()
    if not words:
        return ""
    lines = []
    current = []
    length = 0
    for word in words:
        extra = len(word) + (1 if current else 0)
        if current and length + extra > max_chars:
            lines.append(" ".join(current))
            current = [word]
            length = len(word)
        else:
            current.append(word)
            length += extra
    if current:
        lines.append(" ".join(current))
    if len(lines) <= 2:
        return "\n".join(lines)
    # Preserve all text while keeping the subtitle compact.
    return lines[0] + "\n" + " ".join(lines[1:])


def prepare_subtitles(surah: int, ayah: int, surah_name: str, total_ayahs: int) -> None:
    """Write the current Arabic + English subtitle pair atomically."""
    arabic = _ayah(_AR, surah, ayah)
    english = _ayah(_EN, surah, ayah)
    if not arabic:
        arabic = ""
    if not english:
        english = ""

    # Keep the subtitle itself clean. Metadata is handled by the FFmpeg drawtext
    # layer so it remains stable and readable.
    _atomic_write(SUBTITLE_DIR / "arabic.txt", _wrap_words(arabic, 58))
    _atomic_write(SUBTITLE_DIR / "english.txt", _wrap_words(english, 82))
    _atomic_write(
        SUBTITLE_DIR / "meta.txt",
        f"SURAH {surah_name.upper()}  •  AYAH {ayah} / {total_ayahs}",
    )


def subtitle_files() -> tuple[Path, Path, Path]:
    return (
        SUBTITLE_DIR / "arabic.txt",
        SUBTITLE_DIR / "english.txt",
        SUBTITLE_DIR / "meta.txt",
    )
