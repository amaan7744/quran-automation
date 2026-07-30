#!/usr/bin/env python3
"""
subtitle_builder.py
Documentary-style ASS subtitles: word-level karaoke highlighting on the
Arabic line, smooth fade + pop-in animation, adaptive font sizing, safe
margins, and a strict two-line cap for both Arabic and the translation.

Word timing is estimated by distributing each ayah's known duration
across its words, weighted by character length (a simple, dependency-free
proxy for recitation pacing). For frame-accurate word timing, feed this
module output from a forced aligner (e.g. aeneas/gentle) via the same
`words` structure — see `estimate_word_timings`.

ARABIC SIZING / WRAPPING
-------------------------
Font size is chosen from a closed-form formula calibrated against the
actual glyph metrics of the configured Arabic font. If ARABIC_FONT is ever
changed, PX_PER_CHAR_PER_FONTSIZE below must be re-measured — glyph widths
differ significantly between typefaces.

Very long ayat (e.g. 2:282, ~1170 characters) cannot fit on screen at any
readable size within a two-line cap. Rather than shrinking the font to
illegibility or letting it overflow/clip, such ayat are split into
multiple sequentially-timed "chunks" at word boundaries, each capped at
two lines and shown for the portion of the ayah's audio duration covering
its words (using the same per-word timing used for karaoke highlighting).
This guarantees no clipping and no overlap with the English track
regardless of ayah length.
"""

import textwrap
from pathlib import Path

from config import ARABIC_FONT, ENGLISH_FONT
from logging_utils import get_logger

log = get_logger(__name__)

PLAY_RES_X = 1080
PLAY_RES_Y = 1920

# ── ARABIC LAYOUT TUNING (calibrated for DigitalKhatt IndoPak) ─────────────
# Measured empirically: at Fontsize=90, this font renders ~7.72px per
# character (Unicode codepoint, diacritics counted separately) of shaped
# Quranic text. Re-measure this if ARABIC_FONT is ever changed.
PX_PER_CHAR_PER_FONTSIZE = 7.72 / 90.0
ARABIC_MARGIN_L = 80
ARABIC_MARGIN_R = 80
ARABIC_USABLE_WIDTH = PLAY_RES_X - ARABIC_MARGIN_L - ARABIC_MARGIN_R
ARABIC_MAX_LINES = 2
ARABIC_MIN_FONT_SIZE = 46   # floor for legibility after social-platform re-encoding
ARABIC_MAX_FONT_SIZE = 110  # ceiling so very short ayat don't look oversized
# Safety factor below the theoretical 2-line capacity: real word-wrap breaks
# at word boundaries (not mid-character), so a small buffer avoids a stray
# extra 3rd line when a chunk lands just under the raw pixel limit.
ARABIC_WRAP_SAFETY = 0.92


def arabic_line_capacity(font_size: int) -> int:
    """Max characters (Unicode codepoints) that fit in ARABIC_MAX_LINES
    lines at the given font size, for the configured Arabic font."""
    px_per_char = PX_PER_CHAR_PER_FONTSIZE * font_size
    total_px = ARABIC_MAX_LINES * ARABIC_USABLE_WIDTH * ARABIC_WRAP_SAFETY
    return max(1, int(total_px / px_per_char))


def adaptive_arabic_size(text: str) -> int:
    """
    Picks the largest font size (within [MIN, MAX]) at which `text` fits
    within ARABIC_MAX_LINES lines. This replaces fixed size buckets with a
    formula calibrated to the actual font metrics, so sizing degrades
    smoothly as ayah length grows instead of jumping between a few presets.
    """
    length = len(text)
    if length <= arabic_line_capacity(ARABIC_MAX_FONT_SIZE):
        return ARABIC_MAX_FONT_SIZE
    if length >= arabic_line_capacity(ARABIC_MIN_FONT_SIZE):
        return ARABIC_MIN_FONT_SIZE
    # Solve for the font size whose 2-line capacity equals `length`.
    total_px_budget = ARABIC_MAX_LINES * ARABIC_USABLE_WIDTH * ARABIC_WRAP_SAFETY
    fs = total_px_budget / (PX_PER_CHAR_PER_FONTSIZE * length)
    fs = int(round(fs / 2) * 2)  # round to an even size
    return max(ARABIC_MIN_FONT_SIZE, min(ARABIC_MAX_FONT_SIZE, fs))


# ══════════════════════════════════════════════════════════════════════════
# TEXT HELPERS
# ══════════════════════════════════════════════════════════════════════════

def get_ayah_text(json_data, surah: int, ayah: int) -> str:
    if isinstance(json_data, dict) and "surahs" in json_data:
        for s in json_data["surahs"]:
            if s.get("number") == surah:
                for a in s.get("ayahs", []):
                    if a.get("number") == ayah:
                        return a.get("text", "").strip()
    return ""


def escape_ass(text: str) -> str:
    """Escape characters that are special in ASS dialogue text."""
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def sec_to_ass(s: float) -> str:
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h}:{m:02d}:{sec:05.2f}"


def adaptive_english_size(text: str) -> int:
    length = len(text)
    if length <= 40:
        return 56
    if length <= 90:
        return 46
    return 38


# Calibrated for Poppins at Fontsize=56 (~15.5px/char average across mixed
# word lengths). Used only to decide whether a translation needs to be
# split into multiple synced chunks like the Arabic track — normal-length
# ayat are unaffected and keep the original two-line wrap behavior.
EN_PX_PER_CHAR_PER_FONTSIZE = 15.5 / 56.0
EN_MARGIN_L = 90
EN_MARGIN_R = 90
EN_USABLE_WIDTH = PLAY_RES_X - EN_MARGIN_L - EN_MARGIN_R
EN_MAX_LINES = 2
EN_WRAP_SAFETY = 0.90


def english_line_capacity(font_size: int) -> int:
    px_per_char = EN_PX_PER_CHAR_PER_FONTSIZE * font_size
    total_px = EN_MAX_LINES * EN_USABLE_WIDTH * EN_WRAP_SAFETY
    return max(1, int(total_px / px_per_char))


def wrap_to_two_lines(text: str, width: int) -> str:
    """Wraps text to at most two lines, joined with the ASS newline \\N."""
    lines = textwrap.wrap(text, width=width) or [text]
    if len(lines) > 2:
        # Merge overflow into the second line rather than dropping content —
        # the adaptive font sizing above keeps this rare.
        lines = [lines[0], " ".join(lines[1:])]
    return r"\N".join(lines)


# ══════════════════════════════════════════════════════════════════════════
# KARAOKE WORD TIMING
# ══════════════════════════════════════════════════════════════════════════

def estimate_word_timings(text: str, duration: float) -> list:
    """
    Splits an ayah into words and distributes `duration` across them
    proportional to word length (in centiseconds, as ASS \\k expects).
    Returns list of (word, k_centiseconds).
    """
    words = text.split()
    if not words:
        return []

    weights = [max(len(w), 1) for w in words]
    total_weight = sum(weights)
    total_cs = int(round(duration * 100))

    timings = []
    allocated = 0
    for i, (w, wt) in enumerate(zip(words, weights)):
        if i == len(words) - 1:
            cs = max(total_cs - allocated, 1)
        else:
            cs = max(int(total_cs * wt / total_weight), 1)
            allocated += cs
        timings.append((w, cs))
    return timings


def build_karaoke_text(text: str, duration: float) -> str:
    """Builds an ASS karaoke string like '{\\k37}word1 {\\k42}word2 ...'."""
    timings = estimate_word_timings(text, duration)
    parts = [f"{{\\k{cs}}}{escape_ass(w)}" for w, cs in timings]
    return " ".join(parts)


def chunk_words_by_capacity(words: list, capacity: int) -> list:
    """
    Groups words into chunks whose total character count (incl. joining
    spaces) stays within `capacity`, without ever splitting a word. A
    single word longer than `capacity` is kept alone rather than cut.
    Returns a list of (start_idx, end_idx) index ranges into `words`.
    """
    if not words:
        return []
    chunks = []
    chunk_start = 0
    running_len = 0
    for i, w in enumerate(words):
        add_len = len(w) + (1 if i > chunk_start else 0)
        if running_len + add_len > capacity and i > chunk_start:
            chunks.append((chunk_start, i))
            chunk_start = i
            running_len = len(w)
        else:
            running_len += add_len
    chunks.append((chunk_start, len(words)))
    return chunks


def build_arabic_events(ar_text: str, duration: float, cursor: float, ar_font: str) -> list:
    """
    Returns a list of (start_sec, end_sec, ass_text) for the Arabic line of
    one ayah, relative to the overall subtitle timeline.

    Short/medium ayat: a single event for the whole ayah (unchanged
    behavior from before, just with calibrated sizing).

    Very long ayat: split into multiple word-boundary-safe chunks, each
    capped at ARABIC_MAX_LINES lines, shown sequentially for the portion
    of the ayah's audio duration spanning that chunk's words — so nothing
    is ever clipped and the Arabic block never grows into the English
    track's safe zone.
    """
    font_size = adaptive_arabic_size(ar_text)
    words = ar_text.split()
    capacity = arabic_line_capacity(font_size)

    if len(ar_text) <= capacity:
        # Fits comfortably as a single two-line block.
        karaoke_body = build_karaoke_text(ar_text, duration)
        override = (
            f"{{\\an8\\fad(250,150)\\fscx85\\fscy85"
            f"\\t(0,180,\\fscx100\\fscy100)\\fs{font_size}}}"
        )
        return [(cursor, cursor + duration, override + karaoke_body)]

    # Long ayah: chunk at word boundaries, using per-word timing to derive
    # each chunk's on-screen window within the ayah's audio duration.
    timings = estimate_word_timings(ar_text, duration)  # [(word, cs), ...]
    word_starts_sec = []
    t = 0.0
    for _, cs in timings:
        word_starts_sec.append(t)
        t += cs / 100.0

    ranges = chunk_words_by_capacity(words, capacity)
    events = []
    for ci, (i0, i1) in enumerate(ranges):
        chunk_start_sec = cursor + word_starts_sec[i0]
        if ci + 1 < len(ranges):
            chunk_end_sec = cursor + word_starts_sec[ranges[ci + 1][0]]
        else:
            chunk_end_sec = cursor + duration
        chunk_words = timings[i0:i1]
        karaoke_body = " ".join(
            f"{{\\k{cs}}}{escape_ass(w)}" for w, cs in chunk_words
        )
        override = (
            f"{{\\an8\\fad(200,120)\\fscx90\\fscy90"
            f"\\t(0,150,\\fscx100\\fscy100)\\fs{font_size}}}"
        )
        events.append((chunk_start_sec, chunk_end_sec, override + karaoke_body))

    log.info(
        "Long ayah (%d chars) split into %d timed chunks at fs=%d",
        len(ar_text), len(events), font_size,
    )
    return events


def build_english_events(en_text: str, duration: float, cursor: float, chunk_windows: list = None) -> list:
    """
    Returns a list of (start_sec, end_sec, ass_text) for the English line.

    If the translation fits within EN_MAX_LINES at the adaptive size, it is
    shown as a single event for the full ayah duration (unchanged prior
    behavior). If not, it is split at word boundaries into its own
    capacity-driven chunks (independent of how many Arabic chunks exist,
    since Arabic and English have different character densities) using the
    same word-weighted timing approach as the Arabic karaoke track, so each
    chunk is timed to roughly when those words are being recited and never
    exceeds the two-line safe zone.

    `chunk_windows` is accepted for API symmetry with the Arabic side but
    is no longer required for correctness.
    """
    en_size = adaptive_english_size(en_text)
    capacity = english_line_capacity(en_size)

    if len(en_text) <= capacity:
        wrap_width = max(int(2600 / en_size), 18)
        en_wrapped = wrap_to_two_lines(escape_ass(en_text), wrap_width)
        override = f"{{\\an2\\fad(300,200)\\fs{en_size}}}"
        return [(cursor, cursor + duration, override + en_wrapped)]

    # Too long for two lines even at the smallest adaptive size: chunk at
    # word boundaries using the smallest size for visual consistency across
    # chunks, timed via the same word-weighted distribution used for
    # Arabic karaoke (a reasonable proxy for pacing given we don't have
    # per-word translation alignment).
    chunk_size = en_size
    chunk_capacity = english_line_capacity(chunk_size)
    words = en_text.split()
    ranges = chunk_words_by_capacity(words, chunk_capacity)

    timings = estimate_word_timings(en_text, duration)
    word_starts_sec = []
    t = 0.0
    for _, cs in timings:
        word_starts_sec.append(t)
        t += cs / 100.0

    events = []
    for ci, (i0, i1) in enumerate(ranges):
        win_start = cursor + word_starts_sec[i0]
        win_end = cursor + (word_starts_sec[ranges[ci + 1][0]] if ci + 1 < len(ranges) else duration)
        chunk_text = " ".join(words[i0:i1])
        wrap_width = max(int(2600 / chunk_size), 18)
        en_wrapped = wrap_to_two_lines(escape_ass(chunk_text), wrap_width)
        override = f"{{\\an2\\fad(200,120)\\fs{chunk_size}}}"
        events.append((win_start, win_end, override + en_wrapped))

    log.info(
        "Long translation (%d chars) split into %d timed chunks at fs=%d",
        len(en_text), len(events), chunk_size,
    )
    return events


# ══════════════════════════════════════════════════════════════════════════
# ASS FILE ASSEMBLY
# ══════════════════════════════════════════════════════════════════════════

HEADER_TEMPLATE = """\
[Script Info]
ScriptType: v4.00+
PlayResX: {res_x}
PlayResY: {res_y}
ScaledBorderAndShadow: yes
WrapStyle: 1
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Arabic,{ar_font},110,&H00FFFFFF,&H0000D7FF,&H001A1A1A,&H00000000,0,0,0,0,100,100,0,0,1,5,2,8,80,80,140,1
Style: English,{en_font},56,&H00E8E8E8,&H00E8E8E8,&H001A1A1A,&H00000000,0,0,0,0,100,100,0,0,1,3,1,2,90,90,170,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_subtitles(
    batch: list,
    arabic_data,
    english_data,
    audio_durations: list,
    out_path: Path,
) -> None:
    """
    Writes a full ASS subtitle track:
      - Arabic: karaoke word-highlight, pop-in animation, adaptive size, top-safe zone
      - English: fade-in translation, adaptive size, bottom-safe zone
    Both are capped at two lines and kept within safe margins so they never
    collide with each other or run off the 1080x1920 canvas.
    """
    header = HEADER_TEMPLATE.format(
        res_x=PLAY_RES_X, res_y=PLAY_RES_Y,
        ar_font=ARABIC_FONT, en_font=ENGLISH_FONT,
    )

    events = []
    cursor = 0.0

    for (surah, ayah), duration in zip(batch, audio_durations):
        start = sec_to_ass(cursor)
        end = sec_to_ass(cursor + duration)

        ar_text = get_ayah_text(arabic_data, surah, ayah)
        en_text = get_ayah_text(english_data, surah, ayah)

        ar_chunk_windows = []
        if ar_text:
            ar_chunk_windows = build_arabic_events(ar_text, duration, cursor, ARABIC_FONT)
            for chunk_start, chunk_end, ar_line in ar_chunk_windows:
                events.append(
                    f"Dialogue: 1,{sec_to_ass(chunk_start)},{sec_to_ass(chunk_end)},"
                    f"Arabic,,0,0,0,,{ar_line}"
                )

        if en_text:
            for chunk_start, chunk_end, en_line in build_english_events(
                en_text, duration, cursor, ar_chunk_windows
            ):
                events.append(
                    f"Dialogue: 0,{sec_to_ass(chunk_start)},{sec_to_ass(chunk_end)},"
                    f"English,,0,0,0,,{en_line}"
                )

        cursor += duration

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
        f.write("\n")

    log.info("Subtitle file written -> %s (%d ayat)", out_path.name, len(batch))
