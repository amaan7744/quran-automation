#!/usr/bin/env python3
"""
surah_subtitles.py
ASS subtitles for the long-form (3840x2160, 16:9) pipeline.

Deliberately a DIFFERENT layout model from subtitle_builder.py, not a
resolution-scaled copy of it — reusing its chunked "~3-4 words at a time,
pop-in" reading-group style at long-form length would read exactly like
"a stretched-out Short" (the one thing spec section 1 explicitly rules
out). Long-form Quran videos conventionally show each verse's FULL
Arabic text (wrapped, capped at a few lines) for that verse's entire
recitation window, with its translation beneath it, positioned in the
lower third so the cinematic background stays the visual focus above it
— not a centered block occupying the middle of the frame.

Genuinely reused from subtitle_builder.py (pure helpers, no resolution
assumptions baked in): escape_ass, sec_to_ass, get_ayah_text,
wrap_to_two_lines. Font-size math is NOT reused as-is: subtitle_builder's
PX_PER_CHAR_PER_FONTSIZE constants were measured for its own margin/line
setup; this module re-derives the same per-font ratios against the 4K
canvas's own usable width so wrapping stays accurate at this resolution.
"""

from pathlib import Path

from subtitle_builder import escape_ass, sec_to_ass, get_ayah_text, wrap_to_two_lines
from surah_timeline import SurahTimeline
from config import LONGFORM_ARABIC_FONT, LONGFORM_ENGLISH_FONT
from logging_utils import get_logger

log = get_logger(__name__)


class FontNotAvailableError(RuntimeError):
    pass


def verify_fonts_available(fonts: list = None) -> None:
    """
    Confirms every required font is actually installed (via fontconfig's
    `fc-list`) BEFORE any subtitle/intro/outro/thumbnail rendering runs.
    libass silently substitutes a generic fallback font for any family it
    can't find — for Quranic Arabic text that means wrong glyph shapes,
    missing/incorrect diacritics, and broken shaping with no error at
    all, which is exactly the "silently replace a required Quran font
    with a bad fallback" failure mode this must never allow. Raises
    FontNotAvailableError (never proceeds) if any required font, or
    fontconfig itself, is missing.
    """
    import subprocess
    fonts = fonts or [LONGFORM_ARABIC_FONT, LONGFORM_ENGLISH_FONT]
    try:
        result = subprocess.run(["fc-list"], capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise FontNotAvailableError(
            f"Could not run fontconfig's fc-list to verify required fonts are installed "
            f"({e}). Install fontconfig and the required font files before rendering."
        ) from e
    if result.returncode != 0:
        raise FontNotAvailableError(f"fc-list failed: {result.stderr[-300:]}")

    installed = result.stdout.lower()
    missing = [f for f in fonts if f.lower() not in installed]
    if missing:
        raise FontNotAvailableError(
            f"Required font(s) not installed: {', '.join(missing)}. Refusing to render — "
            f"a missing Quran/Arabic font would silently fall back to a generic font with "
            f"incorrect glyph shaping/diacritics for Quranic text. Install the missing "
            f"font(s) (see the CI workflow for how ARABIC_FONT/ENGLISH_FONT are installed) "
            f"and re-run."
        )
    log.info("Required fonts verified installed: %s", ", ".join(fonts))

PLAY_RES_X = 3840
PLAY_RES_Y = 2160

# ── ARABIC (same per-font px/char ratio subtitle_builder.py measured for
# DigitalKhatt IndoPak — this is a font-intrinsic constant, not tied to any
# particular canvas size, so it carries over unchanged; only the usable
# width/margins below are re-derived for the 4K canvas). ──────────────────
ARABIC_PX_PER_CHAR_PER_FONTSIZE = 7.72 / 90.0
ARABIC_MARGIN_L = 280
ARABIC_MARGIN_R = 280
ARABIC_USABLE_WIDTH = PLAY_RES_X - ARABIC_MARGIN_L - ARABIC_MARGIN_R
ARABIC_MAX_LINES = 3      # a full ayah, not a 3-4 word chunk, so allow more wrap room
ARABIC_MIN_FONT_SIZE = 64
ARABIC_MAX_FONT_SIZE = 118
ARABIC_WRAP_SAFETY = 0.92

EN_PX_PER_CHAR_PER_FONTSIZE = 15.5 / 56.0
EN_MARGIN_L = 320
EN_MARGIN_R = 320
EN_USABLE_WIDTH = PLAY_RES_X - EN_MARGIN_L - EN_MARGIN_R
EN_MAX_LINES = 3
EN_MIN_FONT_SIZE = 40
EN_MAX_FONT_SIZE = 60
EN_WRAP_SAFETY = 0.90

# Lower-third safe area: bottom edge stays clear of YouTube's own UI
# chrome (progress bar / end-screen elements), top edge stays low enough
# that the cinematic background above is never crowded out.
SAFE_BOTTOM_MARGIN = 160
BLOCK_GAP = 26
ARABIC_LINE_HEIGHT_FACTOR = 1.28
EN_LINE_HEIGHT_FACTOR = 1.22
INTRA_BLOCK_LINE_GAP = 10


def _capacity(usable_width: float, safety: float, px_per_char_per_fs: float,
              font_size: int, lines: int) -> int:
    px_per_char = px_per_char_per_fs * font_size
    total_px = lines * usable_width * safety
    return max(1, int(total_px / px_per_char))


def adaptive_arabic_size(text: str) -> int:
    """Largest font size in [MIN,MAX] at which the FULL ayah fits within
    ARABIC_MAX_LINES lines — sized off the whole verse, since the whole
    verse is what's shown at once in this layout (unlike the Shorts
    chunked style)."""
    length = len(text)
    if length == 0:
        return ARABIC_MAX_FONT_SIZE
    cap_at_max = _capacity(ARABIC_USABLE_WIDTH, ARABIC_WRAP_SAFETY,
                            ARABIC_PX_PER_CHAR_PER_FONTSIZE, ARABIC_MAX_FONT_SIZE, ARABIC_MAX_LINES)
    if length <= cap_at_max:
        return ARABIC_MAX_FONT_SIZE
    cap_at_min = _capacity(ARABIC_USABLE_WIDTH, ARABIC_WRAP_SAFETY,
                            ARABIC_PX_PER_CHAR_PER_FONTSIZE, ARABIC_MIN_FONT_SIZE, ARABIC_MAX_LINES)
    if length >= cap_at_min:
        return ARABIC_MIN_FONT_SIZE
    total_px_budget = ARABIC_MAX_LINES * ARABIC_USABLE_WIDTH * ARABIC_WRAP_SAFETY
    fs = total_px_budget / (ARABIC_PX_PER_CHAR_PER_FONTSIZE * length)
    return max(ARABIC_MIN_FONT_SIZE, min(ARABIC_MAX_FONT_SIZE, int(round(fs / 2) * 2)))


def adaptive_english_size(text: str) -> int:
    length = len(text)
    if length == 0:
        return EN_MAX_FONT_SIZE
    cap_at_max = _capacity(EN_USABLE_WIDTH, EN_WRAP_SAFETY,
                            EN_PX_PER_CHAR_PER_FONTSIZE, EN_MAX_FONT_SIZE, EN_MAX_LINES)
    if length <= cap_at_max:
        return EN_MAX_FONT_SIZE
    cap_at_min = _capacity(EN_USABLE_WIDTH, EN_WRAP_SAFETY,
                            EN_PX_PER_CHAR_PER_FONTSIZE, EN_MIN_FONT_SIZE, EN_MAX_LINES)
    if length >= cap_at_min:
        return EN_MIN_FONT_SIZE
    total_px_budget = EN_MAX_LINES * EN_USABLE_WIDTH * EN_WRAP_SAFETY
    fs = total_px_budget / (EN_PX_PER_CHAR_PER_FONTSIZE * length)
    return max(EN_MIN_FONT_SIZE, min(EN_MAX_FONT_SIZE, int(round(fs / 2) * 2)))


def _line_count(text: str, usable_width: float, safety: float, px_per_char_per_fs: float,
                 font_size: int, max_lines: int) -> int:
    one_line_cap = _capacity(usable_width, safety, px_per_char_per_fs, font_size, 1)
    return min(max_lines, max(1, -(-len(text) // max(one_line_cap, 1))))  # ceil div


def _wrap(text: str, usable_width: float, px_per_char_per_fs: float, font_size: int, max_lines: int) -> str:
    px_per_char = px_per_char_per_fs * font_size
    chars_per_line = max(1, int(usable_width / px_per_char))
    lines = []
    words = text.split()
    cur = []
    for w in words:
        trial = " ".join(cur + [w])
        if len(trial) > chars_per_line and cur:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    if len(lines) > max_lines:
        # Merge overflow into the last allowed line rather than dropping text.
        lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    return r"\N".join(lines)


HEADER_TEMPLATE = """\
[Script Info]
ScriptType: v4.00+
PlayResX: {res_x}
PlayResY: {res_y}
ScaledBorderAndShadow: yes
WrapStyle: 2
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Arabic,{ar_font},118,&H00F5F5F5,&H00C9C9C9,&H000A0A0A,&H90000000,0,0,0,0,100,100,0,0,1,3,2,2,{ar_margin_l},{ar_margin_r},{bottom_margin},1
Style: English,{en_font},60,&H00E6F4FF,&H00E6F4FF,&H000A0A0A,&H90000000,0,0,0,0,100,100,0,0,1,2,1.5,2,{en_margin_l},{en_margin_r},{bottom_margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_longform_subtitles(timeline: SurahTimeline, out_path: Path) -> None:
    """
    Writes one ASS subtitle track for the whole Surah: each ayah's full
    Arabic text (wrapped, capped at ARABIC_MAX_LINES) is shown for that
    ayah's entire recitation window (timeline entry start->end, which
    already includes any intro offset), with its full translation
    directly beneath it, both bottom-anchored in the lower-third safe
    area. A verse is never shown early/late and never bleeds into the
    next verse's window (spec section 9), because each event's
    start/end comes directly from the same real per-ayah audio durations
    the recitation itself uses.
    """
    header = HEADER_TEMPLATE.format(
        res_x=PLAY_RES_X, res_y=PLAY_RES_Y,
        ar_font=LONGFORM_ARABIC_FONT, en_font=LONGFORM_ENGLISH_FONT,
        ar_margin_l=ARABIC_MARGIN_L, ar_margin_r=ARABIC_MARGIN_R,
        en_margin_l=EN_MARGIN_L, en_margin_r=EN_MARGIN_R,
        bottom_margin=SAFE_BOTTOM_MARGIN,
    )

    events = []
    for e in timeline.entries:
        if not e.arabic or not e.english:
            continue

        ar_size = adaptive_arabic_size(e.arabic)
        en_size = adaptive_english_size(e.english)
        ar_lines = _line_count(e.arabic, ARABIC_USABLE_WIDTH, ARABIC_WRAP_SAFETY,
                                ARABIC_PX_PER_CHAR_PER_FONTSIZE, ar_size, ARABIC_MAX_LINES)
        en_lines = _line_count(e.english, EN_USABLE_WIDTH, EN_WRAP_SAFETY,
                                EN_PX_PER_CHAR_PER_FONTSIZE, en_size, EN_MAX_LINES)

        ar_h = int(ar_size * ARABIC_LINE_HEIGHT_FACTOR * ar_lines + INTRA_BLOCK_LINE_GAP * max(0, ar_lines - 1))
        en_h = int(en_size * EN_LINE_HEIGHT_FACTOR * en_lines + INTRA_BLOCK_LINE_GAP * max(0, en_lines - 1))

        en_bottom = PLAY_RES_Y - SAFE_BOTTOM_MARGIN
        en_top = en_bottom - en_h
        ar_bottom = en_top - BLOCK_GAP
        ar_top = ar_bottom - ar_h

        ar_text = _wrap(escape_ass(e.arabic), ARABIC_USABLE_WIDTH,
                         ARABIC_PX_PER_CHAR_PER_FONTSIZE, ar_size, ARABIC_MAX_LINES)
        en_text = _wrap(escape_ass(e.english), EN_USABLE_WIDTH,
                         EN_PX_PER_CHAR_PER_FONTSIZE, en_size, EN_MAX_LINES)

        ar_pos = f"\\an8\\pos({PLAY_RES_X // 2},{ar_top})"
        en_pos = f"\\an8\\pos({PLAY_RES_X // 2},{en_top})"
        fade = "\\fad(400,250)"

        events.append(
            f"Dialogue: 1,{sec_to_ass(e.start)},{sec_to_ass(e.end)},Arabic,,0,0,0,,"
            f"{{{ar_pos}{fade}\\fs{ar_size}}}{ar_text}"
        )
        events.append(
            f"Dialogue: 0,{sec_to_ass(e.start)},{sec_to_ass(e.end)},English,,0,0,0,,"
            f"{{{en_pos}{fade}\\fs{en_size}}}{en_text}"
        )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
        f.write("\n")

    log.info("Long-form subtitle file written -> %s (%d ayat)", out_path.name, len(timeline.entries))
