#!/usr/bin/env python3
"""
surah_metadata_gen.py
Builds YouTube title/description/tags/chapters for one Surah, entirely
from dynamic Surah metadata + the real rendered timeline — never
hard-coded to one Surah (spec sections 17-19).
"""

from config import LONGFORM_RECITER_NAME, LONGFORM_TRANSLATION_SOURCE, LONGFORM_CHAPTER_MAX_COUNT
from surah_timeline import SurahTimeline
from logging_utils import get_logger

log = get_logger(__name__)


def build_title(surah_info: dict) -> str:
    return (
        f"Surah {surah_info['name_en']} (Full) | Quran Recitation | "
        f"{LONGFORM_RECITER_NAME} | English Translation"
    )


def _format_ts(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def build_chapters(timeline: SurahTimeline, intro_duration: float) -> list:
    """
    Returns [(timestamp_str, label), ...] starting with "00:00 Introduction".
    These describe positions in the FINAL assembled video (intro + main
    body + outro), so every ayah timestamp is the timeline's
    recitation-relative start time PLUS intro_duration — the timeline
    entries themselves stay recitation-relative (see surah_timeline.py);
    only this function, which is specifically producing FINAL-video
    timestamps, applies the shift.

    One chapter per ayah below LONGFORM_CHAPTER_MAX_COUNT total; above
    that, ayat are grouped into evenly-sized ranges (spec section 19) so
    the description doesn't balloon into hundreds of lines for a Surah
    like Al-Baqarah. Timestamps are the ACTUAL rendered start times pulled
    straight from the timeline — never estimated.
    """
    ayah_count = len(timeline.entries)

    if ayah_count + 1 <= LONGFORM_CHAPTER_MAX_COUNT:
        points = [(0.0, "Introduction")] + timeline.chapter_points(offset=intro_duration)
        return [(_format_ts(t), label) for t, label in points]

    # Group into ranges of roughly equal ayah count so total chapters
    # (including Introduction) stays within the cap.
    group_slots = max(1, LONGFORM_CHAPTER_MAX_COUNT - 1)
    group_size = max(1, -(-ayah_count // group_slots))  # ceil div
    grouped = [(0.0, "Introduction")]
    for i in range(0, ayah_count, group_size):
        chunk = timeline.entries[i:i + group_size]
        start_ayah, end_ayah = chunk[0].ayah, chunk[-1].ayah
        label = f"Ayah {start_ayah}" if start_ayah == end_ayah else f"Ayat {start_ayah}-{end_ayah}"
        grouped.append((chunk[0].start + intro_duration, label))
    return [(_format_ts(t), label) for t, label in grouped]


def build_description(surah_info: dict, chapters: list) -> str:
    lines = [
        f"Surah {surah_info['name_en']} ({surah_info['name_ar']}) — Chapter {surah_info['number']} of the Holy Quran.",
        f"{surah_info['ayah_count']} verses • {surah_info['revelation_type']}",
        "",
        f"Recitation: {LONGFORM_RECITER_NAME}",
        f"Translation: {LONGFORM_TRANSLATION_SOURCE}",
        "",
        "Chapters:",
    ]
    lines.extend(f"{ts} {label}" for ts, label in chapters)
    lines.extend([
        "",
        "Arabic text and English translation are shown throughout for study "
        "and reflection. Background visuals are royalty-free nature footage "
        "sourced via Pexels.",
        "",
        f"#Quran #Islam #{surah_info['name_en'].replace(' ', '')} #QuranRecitation",
    ])
    return "\n".join(lines)


def build_tags(surah_info: dict) -> list:
    base = [
        "Quran", "Islam", "Muslim", "Full Surah", "Quran Recitation",
        "Islamic", "Deen", surah_info["name_en"], f"Surah {surah_info['name_en']}",
        f"Surah {surah_info['number']}", surah_info["revelation_type"], LONGFORM_RECITER_NAME,
        "Quran with English translation", "Quran full surah recitation",
    ]
    # YouTube practical tag budget is ~500 chars total.
    tags, total = [], 0
    for t in base:
        if total + len(t) > 480:
            break
        tags.append(t)
        total += len(t) + 1
    return tags


def build_all_metadata(surah_info: dict, timeline: SurahTimeline, intro_duration: float) -> dict:
    chapters = build_chapters(timeline, intro_duration)
    metadata = {
        "title": build_title(surah_info),
        "chapters": chapters,
        "description": build_description(surah_info, chapters),
        "tags": build_tags(surah_info),
    }
    log.info("Metadata generated: title=%r, %d chapter entries, %d tags",
              metadata["title"], len(chapters), len(metadata["tags"]))
    return metadata
