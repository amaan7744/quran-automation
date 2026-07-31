#!/usr/bin/env python3
"""
subtitle_builder.py
Premium-style ASS subtitles for Quran reels: a single visually centered
subtitle block (Arabic + translation) with true right-to-left karaoke
highlighting, adaptive font sizing, safe margins for Instagram/Facebook/
YouTube Shorts, and a strict two-line cap for both Arabic and the
translation.

Word timing is estimated by distributing each ayah's known duration
across its words, weighted by character length (a simple, dependency-free
proxy for recitation pacing). For frame-accurate word timing, feed this
module output from a forced aligner (e.g. aeneas/gentle) via the same
`words` structure — see `estimate_word_timings`.

LAYOUT
------
Arabic and its translation are treated as one centered subtitle block:
  - The block's vertical center sits slightly above the exact middle of
    the frame (BLOCK_CENTER_Y), which is where the Arabic line naturally
    lands — easy to read, doesn't crowd the top or bottom safe zones.
  - The translation is always positioned directly beneath the Arabic
    block with a fixed gap (BLOCK_GAP), never overlapping.
  - Both pieces use explicit `\\pos` placement computed from each line's
    *actual* estimated height (font size × line count), not fixed style
    margins — so the block stays visually centered and correctly spaced
    whether the Arabic is one line or two, and whether the translation
    is short or long.
  - SAFE_TOP_MARGIN / SAFE_BOTTOM_MARGIN keep the whole block clear of
    platform chrome (username/caption overlays at the top, engagement
    buttons/captions at the bottom) on Reels/Shorts.

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
This guarantees no clipping and no overlap with the translation track
regardless of ayah length.

ARABIC KARAOKE DIRECTION
-------------------------
See `_rtl_karaoke_wrap` for a full explanation of why plain `{\\kNN}word`
sequences visually highlight left-to-right (like English) even though the
Arabic text itself is stored in correct reading order, and why the fix is
to decouple screen position (controlled by word order) from highlight
timing (controlled by explicit `\\t` color transforms), rather than
relying on Unicode bidi control characters, which do not affect libass's
run-by-run placement of `\\k`-separated text.
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


def arabic_line_capacity(font_size: int, lines: int = ARABIC_MAX_LINES) -> int:
    """Max characters (Unicode codepoints) that fit in `lines` lines at the
    given font size, for the configured Arabic font."""
    px_per_char = PX_PER_CHAR_PER_FONTSIZE * font_size
    total_px = lines * ARABIC_USABLE_WIDTH * ARABIC_WRAP_SAFETY
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


def estimate_arabic_line_count(text: str, font_size: int) -> int:
    """Estimates whether `text` will render as 1 or 2 lines at `font_size`
    (Arabic is never allowed past ARABIC_MAX_LINES by construction)."""
    one_line_capacity = arabic_line_capacity(font_size, lines=1)
    return 1 if len(text) <= one_line_capacity else ARABIC_MAX_LINES


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


# Calibrated for Poppins at Fontsize=56 (~15.5px/char average across mixed
# word lengths). Drives both the smooth adaptive sizing below and the
# decision to split a translation into multiple synced chunks like the
# Arabic track — normal-length ayat are unaffected and keep the original
# two-line wrap behavior.
EN_PX_PER_CHAR_PER_FONTSIZE = 15.5 / 56.0
EN_MARGIN_L = 90
EN_MARGIN_R = 90
EN_USABLE_WIDTH = PLAY_RES_X - EN_MARGIN_L - EN_MARGIN_R
EN_MAX_LINES = 2
EN_WRAP_SAFETY = 0.90
EN_MIN_FONT_SIZE = 38  # floor — never too small to read on a phone
EN_MAX_FONT_SIZE = 58  # ceiling for short translations


def english_line_capacity(font_size: int, lines: int = EN_MAX_LINES) -> int:
    px_per_char = EN_PX_PER_CHAR_PER_FONTSIZE * font_size
    total_px = lines * EN_USABLE_WIDTH * EN_WRAP_SAFETY
    return max(1, int(total_px / px_per_char))


def adaptive_english_size(text: str) -> int:
    """
    Smoothly scales translation font size between EN_MIN_FONT_SIZE and
    EN_MAX_FONT_SIZE based on length, the same closed-form approach used
    for Arabic — short ayat get a large, confident size and long ones
    shrink gradually instead of jumping between a few fixed buckets.
    """
    length = len(text)
    if length <= english_line_capacity(EN_MAX_FONT_SIZE):
        return EN_MAX_FONT_SIZE
    if length >= english_line_capacity(EN_MIN_FONT_SIZE):
        return EN_MIN_FONT_SIZE
    total_px_budget = EN_MAX_LINES * EN_USABLE_WIDTH * EN_WRAP_SAFETY
    fs = total_px_budget / (EN_PX_PER_CHAR_PER_FONTSIZE * length)
    fs = int(round(fs / 2) * 2)
    return max(EN_MIN_FONT_SIZE, min(EN_MAX_FONT_SIZE, fs))


def estimate_english_line_count(text: str, font_size: int) -> int:
    one_line_capacity = english_line_capacity(font_size, lines=1)
    return 1 if len(text) <= one_line_capacity else EN_MAX_LINES


def wrap_to_two_lines(text: str, width: int) -> str:
    """Wraps text to at most two lines, joined with the ASS newline \\N."""
    lines = textwrap.wrap(text, width=width) or [text]
    if len(lines) > 2:
        # Merge overflow into the second line rather than dropping content —
        # the adaptive font sizing above keeps this rare.
        lines = [lines[0], " ".join(lines[1:])]
    return r"\N".join(lines)


# ══════════════════════════════════════════════════════════════════════════
# SUBTITLE BLOCK LAYOUT — Arabic + translation centered as one unit
# ══════════════════════════════════════════════════════════════════════════
# Both lines are placed with explicit `\pos` (anchored top-center, \an8)
# rather than relying on style-level MarginV. This lets us compute exact
# pixel positions from each line's *real* estimated height, so the pair
# always reads as one balanced, centered block — Arabic sitting in the
# natural reading zone just above center, translation directly beneath it
# with consistent breathing room — regardless of whether either line is
# one or two lines long.

# Safe zones: keep clear of platform chrome common to Instagram Reels,
# Facebook Reels and YouTube Shorts (profile/caption overlays up top;
# captions, sound title and engagement buttons near the bottom).
SAFE_TOP_MARGIN = 210
SAFE_BOTTOM_MARGIN = 300

# Vertical center of the whole block. Slightly above the frame's exact
# middle (0.5) so the Arabic line lands in the natural "easiest to read"
# zone without ever needing to sit at the very top or bottom.
BLOCK_CENTER_Y = int(PLAY_RES_Y * 0.47)

# Fixed gap between the bottom of the Arabic block and the top of the
# translation block.
BLOCK_GAP = 26

# Approximate line-height multipliers (font size -> px per line). Arabic
# needs more vertical room than the nominal font size suggests because
# Quranic tashkeel (vowel marks) extend above and below the baseline.
ARABIC_LINE_HEIGHT_FACTOR = 1.45
EN_LINE_HEIGHT_FACTOR = 1.30
# Extra breathing room between wrapped lines within the same block.
INTRA_BLOCK_LINE_GAP = 6


def _block_height(font_size: int, n_lines: int, line_factor: float) -> int:
    if not font_size or n_lines <= 0:
        return 0
    return int(font_size * line_factor * n_lines
                + INTRA_BLOCK_LINE_GAP * max(0, n_lines - 1))


def compute_block_positions(ar_font_size, ar_lines, en_font_size, en_lines):
    """
    Returns (arabic_top_y, english_top_y): the \\pos Y coordinates (top
    edge, matching \\an8) for the Arabic and translation lines so that,
    together, they form one block vertically centered on BLOCK_CENTER_Y
    with BLOCK_GAP between them — clamped to stay within the top/bottom
    safe zones without changing the internal spacing.
    """
    ar_h = _block_height(ar_font_size, ar_lines, ARABIC_LINE_HEIGHT_FACTOR)
    en_h = _block_height(en_font_size, en_lines, EN_LINE_HEIGHT_FACTOR)
    gap = BLOCK_GAP if (ar_h and en_h) else 0
    total_h = ar_h + gap + en_h

    block_top = BLOCK_CENTER_Y - total_h // 2
    ar_top = block_top
    en_top = ar_top + ar_h + gap

    # Clamp to the top safe zone, shifting the whole block down together.
    if ar_h and ar_top < SAFE_TOP_MARGIN:
        shift = SAFE_TOP_MARGIN - ar_top
        ar_top += shift
        en_top += shift

    # Clamp to the bottom safe zone, shifting the whole block up together.
    bottom_edge = (en_top + en_h) if en_h else (ar_top + ar_h)
    max_bottom = PLAY_RES_Y - SAFE_BOTTOM_MARGIN
    if bottom_edge > max_bottom:
        shift = bottom_edge - max_bottom
        ar_top -= shift
        en_top -= shift

    return ar_top, en_top


def _arabic_layout(ar_text: str):
    """Returns (font_size, estimated_lines, will_be_chunked) for an ayah's
    Arabic text, or (None, 0, False) if there is no Arabic text."""
    if not ar_text:
        return None, 0, False
    size = adaptive_arabic_size(ar_text)
    capacity = arabic_line_capacity(size)
    chunked = len(ar_text) > capacity
    # Chunked ayat are always rendered as full two-line blocks by
    # construction (see build_arabic_events), so assume 2 lines for
    # positioning — this keeps the block from jumping between chunks.
    lines = ARABIC_MAX_LINES if chunked else estimate_arabic_line_count(ar_text, size)
    return size, lines, chunked


def _english_layout(en_text: str):
    """Returns (font_size, estimated_lines, will_be_chunked) for an ayah's
    translation, or (None, 0, False) if there is no translation text."""
    if not en_text:
        return None, 0, False
    size = adaptive_english_size(en_text)
    capacity = english_line_capacity(size)
    chunked = len(en_text) > capacity
    lines = EN_MAX_LINES if chunked else estimate_english_line_count(en_text, size)
    return size, lines, chunked


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


# ── RTL karaoke ─────────────────────────────────────────────────────────
# Colors used for the manual per-word highlight transform below. These must
# stay in sync with the "Arabic" style's PrimaryColour / SecondaryColour in
# HEADER_TEMPLATE (&H0081B910 / &H00E8E8E8) since we no longer let \k drive
# the Primary/Secondary swap — we set both colors explicitly per word.
ARABIC_KARAOKE_SECONDARY_COLOR = "&H00E8E8E8&"  # resting ("not yet recited")
ARABIC_KARAOKE_PRIMARY_COLOR   = "&H0081B910&"  # highlighted ("being recited")

# Length of the color swap itself (ms). Short enough to read as an instant
# "snap" per word rather than a visible wipe, but non-zero so libass has a
# well-formed \t interval to animate.
KARAOKE_TRANSFORM_MS = 80


def _rtl_karaoke_wrap(word_timings: list) -> str:
    """
    Builds an ASS string for Arabic karaoke that is simultaneously:
      - visually correct (word 1, the first-recited word, ends up on the
        RIGHT, with subsequent words running right-to-left toward the left
        — normal Arabic reading order), and
      - temporally correct (word 1 highlights first, word 2 second, etc.,
        in true recitation order).

    Why not plain `{\\kNN}word`: libass lays out `\\k`-separated runs via a
    left-to-right pen advance *in the order they appear in the source
    string*, and `\\k` also activates highlights strictly in that same
    source order. Both position and timing are driven by the identical
    "order in the string" variable, so there is no way to fix the visual
    left→right bug by simply reordering words under plain `\\k` — doing so
    would just as surely reverse the highlight sequence, since the same
    reordering feeds both mechanisms at once. This is also why wrapping
    the text in RLE/RLM/PDF bidi-control characters has no effect: those
    only influence bidi-level computation for glyph shaping/mirroring
    *within* a run, not libass's run-by-run placement across separate
    `\\k`-delimited runs.

    The fix decouples the two:
      - Screen position is controlled by literally reversing the WORD
        order in the emitted string (word_N ... word_1). Since libass
        advances the pen left-to-right through the string, the *last*
        word written ends up *rightmost* on screen — so writing word 1
        last places it correctly on the right. Only whole-word order
        changes; each word's internal character sequence — and therefore
        its Arabic shaping, letter joining, and ligatures via HarfBuzz —
        is left completely untouched, so nothing is reversed at the
        letter level.
      - Highlight timing is controlled by an explicit `\\t(t1,t2,\\cXXXXXX)`
        color transform per word, using absolute millisecond offsets
        relative to this Dialogue line's own start time — not `\\k`'s
        cumulative, order-dependent duration. Word 1 always gets t1=0
        regardless of where it physically sits in the string, so its
        highlight fires first no matter its screen position.

    Net effect: word 1 is rightmost AND lights up first; each following
    word appears progressively further left and lights up progressively
    later — a highlight that visibly travels right-to-left, matching real
    Quran recitation, with correct Arabic shaping preserved throughout.
    """
    if not word_timings:
        return ""

    # Compute each word's absolute highlight window in true recitation
    # order first (word 1 = word_timings[0] gets the earliest window).
    timed_words = []
    cumulative_ms = 0
    for word, cs in word_timings:
        start_ms = cumulative_ms
        duration_ms = cs * 10  # \k centiseconds -> milliseconds
        end_ms = start_ms + min(duration_ms, KARAOKE_TRANSFORM_MS)
        timed_words.append((word, start_ms, end_ms))
        cumulative_ms += duration_ms

    # Only NOW reverse for display: last-recited word first in the string,
    # first-recited word last — so word 1 ends up rightmost on screen.
    # Its (start_ms, end_ms) travel with it, so timing is unaffected.
    parts = [
        f"{{\\c{ARABIC_KARAOKE_SECONDARY_COLOR}"
        f"\\t({start_ms},{end_ms},\\c{ARABIC_KARAOKE_PRIMARY_COLOR})}}"
        f"{escape_ass(word)}"
        for word, start_ms, end_ms in reversed(timed_words)
    ]
    return " ".join(parts)


def build_karaoke_text(text: str, duration: float) -> str:
    """Builds a right-to-left ASS karaoke string for a full ayah."""
    timings = estimate_word_timings(text, duration)
    return _rtl_karaoke_wrap(timings)


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


def build_arabic_events(ar_text: str, duration: float, cursor: float,
                         font_size: int, pos_y: int) -> list:
    """
    Returns a list of (start_sec, end_sec, ass_text) for the Arabic line of
    one ayah, relative to the overall subtitle timeline.

    Short/medium ayat: a single event for the whole ayah.

    Very long ayat: split into multiple word-boundary-safe chunks, each
    capped at ARABIC_MAX_LINES lines, shown sequentially for the portion
    of the ayah's audio duration spanning that chunk's words — so nothing
    is ever clipped and the Arabic block never grows into the
    translation's safe zone.

    `font_size` and `pos_y` are pre-computed by the caller (via
    `_arabic_layout` / `compute_block_positions`) so every chunk of a
    given ayah shares the same size and screen position — the block
    stays put instead of jumping around while a long ayah plays.
    """
    words = ar_text.split()
    capacity = arabic_line_capacity(font_size)
    pos = f"\\an8\\pos({PLAY_RES_X // 2},{pos_y})"

    if len(ar_text) <= capacity:
        # Fits comfortably as a single two-line block.
        karaoke_body = build_karaoke_text(ar_text, duration)
        override = (
            f"{{{pos}\\fad(300,200)\\fscx94\\fscy94"
            f"\\t(0,220,2,\\fscx100\\fscy100)\\blur2\\fs{font_size}}}"
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
        karaoke_body = _rtl_karaoke_wrap(chunk_words)
        override = (
            f"{{{pos}\\fad(220,140)\\fscx96\\fscy96"
            f"\\t(0,180,2,\\fscx100\\fscy100)\\blur2\\fs{font_size}}}"
        )
        events.append((chunk_start_sec, chunk_end_sec, override + karaoke_body))

    log.info(
        "Long ayah (%d chars) split into %d timed chunks at fs=%d",
        len(ar_text), len(events), font_size,
    )
    return events


def build_english_events(en_text: str, duration: float, cursor: float,
                          font_size: int, pos_y: int) -> list:
    """
    Returns a list of (start_sec, end_sec, ass_text) for the translation
    line.

    If the translation fits within EN_MAX_LINES at the adaptive size, it is
    shown as a single event for the full ayah duration. If not, it is
    split at word boundaries into its own capacity-driven chunks
    (independent of how many Arabic chunks exist, since Arabic and English
    have different character densities) using the same word-weighted
    timing approach as the Arabic karaoke track, so each chunk is timed to
    roughly when those words are being recited and never exceeds the
    two-line safe zone.

    `font_size` and `pos_y` are pre-computed by the caller so every chunk
    shares the same size and screen position, staying locked directly
    beneath the Arabic block.
    """
    capacity = english_line_capacity(font_size)
    pos = f"\\an8\\pos({PLAY_RES_X // 2},{pos_y})"

    if len(en_text) <= capacity:
        wrap_width = max(int(2600 / font_size), 18)
        en_wrapped = wrap_to_two_lines(escape_ass(en_text), wrap_width)
        override = (
            f"{{{pos}\\fad(320,220)\\fscx97\\fscy97"
            f"\\t(0,240,2,\\fscx100\\fscy100)\\blur1\\fs{font_size}}}"
        )
        return [(cursor, cursor + duration, override + en_wrapped)]

    # Too long for two lines even at the smallest adaptive size: chunk at
    # word boundaries, timed via the same word-weighted distribution used
    # for Arabic karaoke (a reasonable proxy for pacing given we don't
    # have per-word translation alignment).
    words = en_text.split()
    ranges = chunk_words_by_capacity(words, capacity)

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
        wrap_width = max(int(2600 / font_size), 18)
        en_wrapped = wrap_to_two_lines(escape_ass(chunk_text), wrap_width)
        override = (
            f"{{{pos}\\fad(200,120)\\fscx98\\fscy98"
            f"\\t(0,180,2,\\fscx100\\fscy100)\\blur1\\fs{font_size}}}"
        )
        events.append((win_start, win_end, override + en_wrapped))

    log.info(
        "Long translation (%d chars) split into %d timed chunks at fs=%d",
        len(en_text), len(events), font_size,
    )
    return events


# ══════════════════════════════════════════════════════════════════════════
# ASS FILE ASSEMBLY
# ══════════════════════════════════════════════════════════════════════════

# Colors (ASS format &HAABBGGRR):
#   Arabic SecondaryColour = soft ivory/grey  (E8E8E8) — the "not yet
#     recited" resting color, calm and readable on any background.
#   Arabic PrimaryColour   = emerald          (10B981) — the karaoke
#     highlight color a word switches to the instant it's recited.
#   OutlineColour is a deep near-black for contrast on any footage;
#   BackColour carries a soft, partially-transparent shadow (not a hard
#   flat one) so the glow reads as premium rather than a cheap subtitle box.
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
Style: Arabic,{ar_font},110,&H0081B910,&H00E8E8E8,&H00141414,&H50000000,0,0,0,0,100,100,0,0,1,4,2,8,80,80,140,1
Style: English,{en_font},56,&H00EDEDED,&H00EDEDED,&H00141414,&H60000000,0,0,0,0,100,100,0,0,1,3,1,8,90,90,170,1

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
      - Arabic: right-to-left karaoke word-highlight, gentle fade + scale
        animation, adaptive size, positioned in the natural reading zone
        just above screen center.
      - Translation: fade-in, adaptive size, positioned directly beneath
        the Arabic with consistent spacing.
    Arabic + translation are laid out as a single centered block (see
    compute_block_positions) and both are capped at two lines, so they
    never collide with each other or run off the 1080x1920 canvas, and
    stay clear of Reels/Shorts safe zones.
    """
    header = HEADER_TEMPLATE.format(
        res_x=PLAY_RES_X, res_y=PLAY_RES_Y,
        ar_font=ARABIC_FONT, en_font=ENGLISH_FONT,
    )

    events = []
    cursor = 0.0

    for (surah, ayah), duration in zip(batch, audio_durations):
        ar_text = get_ayah_text(arabic_data, surah, ayah)
        en_text = get_ayah_text(english_data, surah, ayah)

        ar_size, ar_lines, _ = _arabic_layout(ar_text)
        en_size, en_lines, _ = _english_layout(en_text)
        ar_top, en_top = compute_block_positions(ar_size, ar_lines, en_size, en_lines)

        if ar_text:
            for chunk_start, chunk_end, ar_line in build_arabic_events(
                ar_text, duration, cursor, ar_size, ar_top
            ):
                events.append(
                    f"Dialogue: 1,{sec_to_ass(chunk_start)},{sec_to_ass(chunk_end)},"
                    f"Arabic,,0,0,0,,{ar_line}"
                )

        if en_text:
            for chunk_start, chunk_end, en_line in build_english_events(
                en_text, duration, cursor, en_size, en_top
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
