#!/usr/bin/env python3
"""
subtitle_builder.py
Premium-style ASS subtitles for Quran reels: a single visually centered
subtitle block (Arabic + translation), adaptive font sizing, safe margins
for Instagram/Facebook/YouTube Shorts, and a strict two-line cap for both
Arabic and the translation.

Word timing is estimated by distributing each ayah's known duration
across its words, weighted by character length (a simple, dependency-free
proxy for recitation pacing), purely to determine when each reading-group
chunk starts and ends on screen — text within a chunk is shown statically,
not highlighted word-by-word. For frame-accurate chunk timing, feed this
module output from a forced aligner (e.g. aeneas/gentle) via the same
`words` structure — see `estimate_word_timings`.

LAYOUT
------
Arabic and its translation are treated as one centered subtitle block:
  - The block's vertical center sits slightly above the exact middle of
    the frame (BLOCK_CENTER_Y, ~47% of frame height), which is where the
    Arabic line naturally lands — easy to read, doesn't crowd the top or
    bottom safe zones.
  - The translation is always positioned directly beneath the Arabic
    block with a fixed gap (BLOCK_GAP), never overlapping.
  - Both pieces use explicit `\\pos` placement computed from each line's
    *actual* estimated height (font size × line count), not fixed style
    margins — so the block stays visually centered and correctly spaced
    for however many lines a given reading chunk actually renders as.
  - SAFE_TOP_MARGIN / SAFE_BOTTOM_MARGIN keep the whole block clear of
    platform chrome (username/caption overlays at the top, engagement
    buttons/captions at the bottom) on Reels/Shorts.
  - Position is computed ONCE per ayah (not per chunk), so the block
    holds still at a single (x, y) for the whole ayah while individual
    reading chunks fade in and out inside it — no jumping mid-ayah.

CHUNKED READING (premium-reel style)
-------------------------------------
Rather than ever showing a full ayah at once, the Arabic is split into
natural reading groups of ~3-4 words (ARABIC_CHUNK_TARGET_WORDS). Each
group is shown, its words highlight in recitation order, and then the
next group replaces it — continuing until the ayah finishes. This keeps
the screen uncluttered and lets a viewer comfortably read and listen at
the same time, matching how professional Quran-reel channels caption.

The translation is split into aligned chunks that are shown underneath
each Arabic reading group for (approximately) the same time window. We
don't have true word-level Arabic<->English alignment, so translation
words are distributed across chunks by matching each Arabic chunk's
share of the ayah's total duration to that same share of the English
word count (see `_map_translation_chunks`). As a safety net, if a mapped
translation chunk is still too long to fit in two lines at the chosen
font size (verbose translations of terse Arabic), it is further split at
capacity boundaries so nothing ever clips or overflows.

ARABIC SIZING / WRAPPING
-------------------------
Font size is chosen from a closed-form formula calibrated against the
actual glyph metrics of the configured Arabic font. If ARABIC_FONT is ever
changed, PX_PER_CHAR_PER_FONTSIZE below must be re-measured — glyph widths
differ significantly between typefaces.

Sizing is driven by the *longest individual reading group* the ayah will
actually be split into (~3-4 words, see `chunk_words_fixed_count`), not
the full ayah's character length. Every chunk in a given ayah still
shares this one size, so nothing jumps mid-ayah — but the size itself now
reflects what's actually on screen at once. Sizing off the whole ayah
instead (the previous behavior) meant a long ayah's short reading groups
still inherited a shrunken font chosen to fit the *entire* recitation
into two lines, even though only ~3-4 words are ever shown at a time —
this was the main cause of subtitles reading as undersized. Because each
chunk is short, the two-line cap is essentially never a binding
constraint in practice; a capacity safety net still exists for the rare
case of unusually long words.

The translation's sizing mirrors this: `adaptive_english_size` sizes off
the longest individual translation chunk aligned to those same Arabic
reading groups (see `_map_translation_chunks`), not the full translation
text, for the identical reason.
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
# Quranic text. Re-measure this if ARABIC_FONT is ever changed. This is a
# per-unit (px per char per fontsize) ratio, so it scales linearly with
# font size and does not need re-measuring when only MIN/MAX bounds change.
PX_PER_CHAR_PER_FONTSIZE = 7.72 / 90.0
ARABIC_MARGIN_L = 80
ARABIC_MARGIN_R = 80
ARABIC_USABLE_WIDTH = PLAY_RES_X - ARABIC_MARGIN_L - ARABIC_MARGIN_R
ARABIC_MAX_LINES = 2
# Sizes increased again now that sizing is computed off the longest
# on-screen reading group instead of the full ayah (see adaptive_arabic_size
# below) — with that fix, MAX is what nearly every ayah renders at, since a
# 3-4 word chunk is almost never close to the two-line capacity limit. This
# is the primary lever for how large the Arabic reads relative to the
# reference screenshot; nudge it up/down after a visual check on real
# footage, since exact px-per-char-to-on-screen-% mapping needs a render to
# confirm precisely.
ARABIC_MIN_FONT_SIZE = 72   # floor for legibility after social-platform re-encoding
ARABIC_MAX_FONT_SIZE = 190  # ceiling — the size almost all chunks now use
# Safety factor below the theoretical 2-line capacity: real word-wrap breaks
# at word boundaries (not mid-character), so a small buffer avoids a stray
# extra 3rd line when a chunk lands just under the raw pixel limit.
ARABIC_WRAP_SAFETY = 0.92

# ── CHUNKED READING GROUPS ─────────────────────────────────────────────────
# Instead of ever showing a full ayah at once, Arabic is split into small
# reading groups so the viewer can comfortably read + listen in sync.
ARABIC_CHUNK_TARGET_WORDS = 4   # aim for ~4 words per on-screen group
ARABIC_CHUNK_MIN_WORDS = 3      # never leave a lone 1-2 word orphan chunk


def arabic_line_capacity(font_size: int, lines: int = ARABIC_MAX_LINES) -> int:
    """Max characters (Unicode codepoints) that fit in `lines` lines at the
    given font size, for the configured Arabic font."""
    px_per_char = PX_PER_CHAR_PER_FONTSIZE * font_size
    total_px = lines * ARABIC_USABLE_WIDTH * ARABIC_WRAP_SAFETY
    return max(1, int(total_px / px_per_char))


def _longest_arabic_chunk_length(text: str) -> int:
    """Character length of the longest individual reading group `text`
    will actually be split into (see `chunk_words_fixed_count`) — i.e.
    the most that's ever shown on screen at once for this ayah. This is
    what Arabic sizing should be measured against, not the full ayah."""
    words = text.split()
    if not words:
        return 0
    ranges = chunk_words_fixed_count(words)
    return max(len(" ".join(words[i0:i1])) for i0, i1 in ranges)


def adaptive_arabic_size(text: str) -> int:
    """
    Picks the largest font size (within [MIN, MAX]) at which the ayah's
    *longest individual reading group* — the most that's ever actually
    shown on screen at once, ~3-4 words, see `chunk_words_fixed_count` —
    fits within ARABIC_MAX_LINES lines. Every chunk in a given ayah still
    shares this one size (no jumping mid-ayah), but the size now reflects
    what's on screen rather than the full recitation, so long ayat no
    longer render their short reading groups undersized.
    """
    length = _longest_arabic_chunk_length(text)
    if length == 0:
        return ARABIC_MAX_FONT_SIZE
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
# capacity safety-net for translation chunks that run long relative to
# their aligned Arabic reading group.
EN_PX_PER_CHAR_PER_FONTSIZE = 15.5 / 56.0
EN_MARGIN_L = 90
EN_MARGIN_R = 90
EN_USABLE_WIDTH = PLAY_RES_X - EN_MARGIN_L - EN_MARGIN_R
EN_MAX_LINES = 2
EN_WRAP_SAFETY = 0.90
# Sizes increased slightly (~8%) so the translation is a touch more
# readable while remaining clearly secondary to the (much larger) Arabic
# above it.
EN_MIN_FONT_SIZE = 50  # floor — never too small to read on a phone
EN_MAX_FONT_SIZE = 76  # ceiling for short translation chunks


def english_line_capacity(font_size: int, lines: int = EN_MAX_LINES) -> int:
    px_per_char = EN_PX_PER_CHAR_PER_FONTSIZE * font_size
    total_px = lines * EN_USABLE_WIDTH * EN_WRAP_SAFETY
    return max(1, int(total_px / px_per_char))


def _longest_mapped_translation_length(en_text: str, ar_chunk_windows: list,
                                        duration: float) -> int:
    """Character length of the longest translation chunk that will
    actually be shown on screen at once, once `en_text` is mapped onto
    `ar_chunk_windows` (see `_map_translation_chunks`). Falls back to the
    full text length if there's nothing to map against."""
    words = en_text.split()
    if not words or not ar_chunk_windows:
        return len(en_text)
    mapped = _map_translation_chunks(words, ar_chunk_windows, duration)
    if not mapped:
        return len(en_text)
    return max(len(" ".join(words[i0:i1])) for i0, i1, _, _ in mapped)


def adaptive_english_size(text: str, ar_chunk_windows: list = None,
                           duration: float = None) -> int:
    """
    Scales translation font size between EN_MIN_FONT_SIZE and
    EN_MAX_FONT_SIZE off the longest individual translation chunk that
    will actually appear on screen at once — aligned to the Arabic
    reading groups via `ar_chunk_windows`, mirroring the same
    longest-chunk-not-whole-ayah fix applied to `adaptive_arabic_size` and
    for the same reason: a long ayah's full translation is never shown at
    once, so sizing off it needlessly shrinks every on-screen chunk. If
    `ar_chunk_windows` isn't available yet, falls back to sizing off the
    whole text.
    """
    if ar_chunk_windows:
        length = _longest_mapped_translation_length(text, ar_chunk_windows, duration)
    else:
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
# with consistent breathing room — regardless of how many lines a given
# reading chunk renders as.

# Safe zones: keep clear of platform chrome common to Instagram Reels,
# Facebook Reels and YouTube Shorts (profile/caption overlays up top;
# captions, sound title and engagement buttons near the bottom).
SAFE_TOP_MARGIN = 210
SAFE_BOTTOM_MARGIN = 300

# Vertical center of the whole block: ~47% of frame height. Slightly above
# the frame's exact middle so the Arabic line lands in the natural
# "easiest to read" zone (within the requested 45-50% band) without ever
# needing to sit at the very top or bottom.
BLOCK_CENTER_Y = int(PLAY_RES_Y * 0.47)

# Fixed gap between the bottom of the Arabic block and the top of the
# translation block. Kept small so the translation sits snugly under the
# Arabic — enough breathing room that the two never touch, not the wide
# gap of a first pass.
BLOCK_GAP = 12

# Approximate line-height multipliers (font size -> px per line). Arabic
# needs more vertical room than the nominal font size suggests because
# Quranic tashkeel (vowel marks) extend above and below the baseline.
ARABIC_LINE_HEIGHT_FACTOR = 1.30
EN_LINE_HEIGHT_FACTOR = 1.20
# Extra breathing room between wrapped lines within the same block.
INTRA_BLOCK_LINE_GAP = 2

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


# ══════════════════════════════════════════════════════════════════════════
# WORD TIMING — used only to compute each reading group's start/end
# ══════════════════════════════════════════════════════════════════════════
# Words are still timed individually (weighted by character length) so
# that a reading group's on-screen window is proportional to how long its
# words actually take to recite — but the words themselves are displayed
# statically as one static block of text, not highlighted one at a time.

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


def _cumulative_word_times(word_timings: list) -> list:
    """Given [(word, cs), ...], returns cumulative second-boundaries of
    length len(word_timings)+1: boundaries[i] is the start time (seconds,
    relative to the ayah's own start) of word i, and boundaries[-1] is the
    ayah's total duration."""
    boundaries = [0.0]
    t = 0.0
    for _, cs in word_timings:
        t += cs / 100.0
        boundaries.append(t)
    return boundaries


def chunk_words_by_capacity(words: list, capacity: int) -> list:
    """
    Groups words into chunks whose total character count (incl. joining
    spaces) stays within `capacity`, without ever splitting a word. A
    single word longer than `capacity` is kept alone rather than cut.
    Returns a list of (start_idx, end_idx) index ranges into `words`.

    Used only as a safety net (see `_enforce_capacity` and the
    translation overflow handling in `plan_english_chunks`) — the primary
    chunking strategy is the fixed ~3-4 word reading-group split below.
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


def chunk_words_fixed_count(words: list, target: int = ARABIC_CHUNK_TARGET_WORDS,
                             min_size: int = ARABIC_CHUNK_MIN_WORDS) -> list:
    """
    Splits `words` into reading groups of roughly `target` words each,
    avoiding a lone trailing orphan chunk smaller than `min_size` by
    rebalancing the last one or two groups. This is the primary Arabic
    chunking strategy: every ayah — short or long — is always broken into
    ~3-4 word groups rather than ever shown in full.
    """
    n = len(words)
    if n == 0:
        return []
    if n <= target:
        return [(0, n)]
    if n <= target + min_size - 1:
        # Too few words left over for a clean target-sized chunk plus a
        # separate min-sized one: split into two balanced halves instead.
        half = (n + 1) // 2
        return [(0, half), (half, n)]

    ranges = []
    start = 0
    while n - start > target:
        remaining_after = n - (start + target)
        if 0 < remaining_after < min_size:
            # Shrink this chunk slightly so the final one isn't an orphan.
            take = max(min_size, target - (min_size - remaining_after))
        else:
            take = target
        ranges.append((start, start + take))
        start += take
    ranges.append((start, n))
    return ranges


def _enforce_capacity(words: list, ranges: list, capacity: int) -> list:
    """
    Safety net: if a fixed-size reading-group's text would still exceed
    the 2-line pixel capacity at the chosen font size (e.g. an unusually
    long word), further split just that chunk at capacity boundaries so
    nothing ever clips. In practice this almost never triggers, since
    ~3-4 words is far below capacity at the enlarged font sizes used here.
    """
    fixed = []
    for i0, i1 in ranges:
        chunk_words = words[i0:i1]
        if len(" ".join(chunk_words)) <= capacity:
            fixed.append((i0, i1))
            continue
        for a, b in chunk_words_by_capacity(chunk_words, capacity):
            fixed.append((i0 + a, i0 + b))
    return fixed


def _split_words_evenly_by_weight(words: list, t_start: float, t_end: float) -> list:
    """Distributes the time window [t_start, t_end] across `words`
    proportional to character length, returning cumulative second-
    boundaries of length len(words)+1. Used to time a translation chunk
    that had to be further split because it overflowed its aligned
    Arabic chunk's on-screen window."""
    if not words:
        return [t_start, t_end]
    weights = [max(len(w), 1) for w in words]
    total = sum(weights)
    span = t_end - t_start
    boundaries = [t_start]
    acc = 0
    for w in weights:
        acc += w
        boundaries.append(t_start + span * acc / total)
    return boundaries


def _map_translation_chunks(en_words: list, ar_chunk_windows: list,
                             ayah_duration: float) -> list:
    """
    Splits `en_words` into chunks aligned to the timing windows of the
    Arabic reading groups (ar_chunk_windows), so each translation chunk
    is shown under the Arabic words it translates for roughly the same
    window. There is no true word-level Arabic<->English alignment
    available, so words are distributed by matching each Arabic chunk's
    share of the ayah's total duration to that same share of the English
    word count.

    If a given Arabic window's time-share rounds down to zero new English
    words (e.g. a very short Arabic group against a terse translation),
    that window is folded into the previous translation chunk (extending
    its end time) rather than leaving a gap with nothing shown.

    Returns a list of (start_idx, end_idx, start_sec, end_sec), all times
    relative to the ayah's own start.
    """
    n_words = len(en_words)
    if n_words == 0 or not ar_chunk_windows or ayah_duration <= 0:
        return []

    chunks = []
    prev_idx = 0
    prev_start = ar_chunk_windows[0][0]
    n_windows = len(ar_chunk_windows)

    for i, (_, t_end) in enumerate(ar_chunk_windows):
        if i == n_windows - 1:
            idx_end = n_words
        else:
            frac = min(1.0, t_end / ayah_duration)
            idx_end = max(prev_idx, min(n_words, round(frac * n_words)))

        if idx_end > prev_idx:
            chunks.append((prev_idx, idx_end, prev_start, t_end))
            prev_idx = idx_end
            prev_start = t_end
        elif chunks:
            # No new English words fall in this Arabic window — keep
            # showing the previous translation chunk through it.
            w0, w1, ts, _te = chunks[-1]
            chunks[-1] = (w0, w1, ts, t_end)

    return chunks


# ══════════════════════════════════════════════════════════════════════════
# CHUNK PLANNING — pure timing/line-count planning, no ASS/position yet
# ══════════════════════════════════════════════════════════════════════════
# Planning is split from rendering so the block's vertical position can be
# computed once from each chunk's *actual* line count before any \pos tags
# are written (see build_subtitles).

def plan_arabic_chunks(ar_text: str, duration: float, font_size: int) -> list:
    """Returns a list of dicts: {start, end, text, lines} — one per
    Arabic reading group, times relative to the ayah's own start. Words
    are still timed individually (see estimate_word_timings) purely to
    give each reading group a start/end proportional to its recitation
    length — the group's words are then shown as one static block of
    text, not highlighted individually."""
    words = ar_text.split()
    if not words:
        return []

    capacity = arabic_line_capacity(font_size)
    ranges = _enforce_capacity(
        words, chunk_words_fixed_count(words), capacity
    )
    word_timings = estimate_word_timings(ar_text, duration)
    boundaries = _cumulative_word_times(word_timings)

    plan = []
    for i0, i1 in ranges:
        plan.append({
            "start": boundaries[i0],
            "end": boundaries[i1],
            "text": " ".join(words[i0:i1]),
            "lines": estimate_arabic_line_count(" ".join(words[i0:i1]), font_size),
        })
    return plan


def plan_english_chunks(en_text: str, duration: float, font_size: int,
                         ar_chunk_windows: list) -> list:
    """Returns a list of dicts: {start, end, text, lines} — one per
    translation chunk, aligned to `ar_chunk_windows` (times relative to
    the ayah's own start)."""
    words = en_text.split()
    if not words or not ar_chunk_windows:
        return []

    capacity = english_line_capacity(font_size)
    mapped = _map_translation_chunks(words, ar_chunk_windows, duration)

    plan = []
    for i0, i1, win_start, win_end in mapped:
        sub_words = words[i0:i1]
        chunk_text = " ".join(sub_words)

        if len(chunk_text) <= capacity:
            spans = [(sub_words, win_start, win_end)]
        else:
            # Safety net: this translation chunk is too long for its
            # aligned Arabic window (verbose translation of a terse
            # phrase) — split further at capacity boundaries, timed
            # proportionally within the same window.
            sub_ranges = chunk_words_by_capacity(sub_words, capacity)
            sub_boundaries = _split_words_evenly_by_weight(sub_words, win_start, win_end)
            spans = [
                (sub_words[a:b], sub_boundaries[a], sub_boundaries[b])
                for a, b in sub_ranges
            ]

        for words_span, t_start, t_end in spans:
            text = " ".join(words_span)
            plan.append({
                "start": t_start,
                "end": t_end,
                "text": text,
                "lines": estimate_english_line_count(text, font_size),
            })
    return plan


# ══════════════════════════════════════════════════════════════════════════
# ASS FILE ASSEMBLY
# ══════════════════════════════════════════════════════════════════════════

# Colors (ASS format &HAABBGGRR):
#   Arabic  = soft white  (F5F5F5) — set once via the style's
#     PrimaryColour; text is shown statically, not word-highlighted.
#   Translation color = warm white (E6F4FF-family) — secondary to the
#     Arabic, but with a touch of warmth rather than clinical grey.
#   OutlineColour is a deep near-black for contrast on any footage, kept
#   thin (Outline field below) rather than a thick border; BackColour
#   carries a soft, mostly-transparent shadow (not a hard flat one or a
#   heavy glow) so it reads as premium rather than a cheap subtitle box.
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
Style: Arabic,{ar_font},190,&H00F5F5F5,&H00C9C9C9,&H00141414,&H70000000,0,0,0,0,100,100,0,0,1,2,1.5,8,80,80,140,1
Style: English,{en_font},76,&H00E6F4FF,&H00E6F4FF,&H00141414,&H70000000,0,0,0,0,100,100,0,0,1,1.5,1,8,90,90,170,1

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
    Writes a full ASS subtitle track. For every ayah:
      - Arabic is split into ~3-4 word reading groups, each shown in turn
        as one static block of text (no per-word highlighting) with a
        subtle fade in/out.
      - The translation is split into chunks aligned to the same reading
        groups' timing and shown directly beneath them.
    Arabic + translation are laid out as a single centered block (see
    compute_block_positions), sized and positioned once per ayah so the
    block holds still while chunks fade in/out inside it, capped at two
    lines each so nothing ever collides or runs off the 1080x1920 canvas,
    and kept clear of Reels/Shorts safe zones throughout.
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

        ar_size = adaptive_arabic_size(ar_text) if ar_text else None
        ar_plan = plan_arabic_chunks(ar_text, duration, ar_size) if ar_text else []
        ar_windows = [(c["start"], c["end"]) for c in ar_plan]

        # English sizing needs ar_windows (it sizes off the longest
        # translation chunk aligned to the Arabic reading groups), so it's
        # computed after the Arabic plan rather than in parallel with it.
        en_size = (
            adaptive_english_size(en_text, ar_windows, duration)
            if en_text else None
        )
        en_plan = (
            plan_english_chunks(en_text, duration, en_size, ar_windows)
            if en_text else []
        )

        ar_lines = max((c["lines"] for c in ar_plan), default=0)
        en_lines = max((c["lines"] for c in en_plan), default=0)
        ar_top, en_top = compute_block_positions(ar_size, ar_lines, en_size, en_lines)

        if ar_plan:
            pos = f"\\an8\\pos({PLAY_RES_X // 2},{ar_top})"
            for c in ar_plan:
                ar_body = escape_ass(c["text"])
                # Pure fade in/out only — no scale-pop, no per-word
                # highlighting. \blur1 gives a soft shadow feel without
                # reading as glow. Plain text in logical (reading) order
                # renders correctly right-to-left via libass's normal bidi
                # handling — the word-reversal trick was only needed for
                # the old \k-driven karaoke, not for static text.
                override = f"{{{pos}\\fad(260,160)\\blur1\\fs{ar_size}}}"
                start = cursor + c["start"]
                end = cursor + c["end"]
                events.append(
                    f"Dialogue: 1,{sec_to_ass(start)},{sec_to_ass(end)},"
                    f"Arabic,,0,0,0,,{override + ar_body}"
                )

        if en_plan:
            pos = f"\\an8\\pos({PLAY_RES_X // 2},{en_top})"
            wrap_width = max(int(2600 / en_size), 18)
            for c in en_plan:
                en_wrapped = wrap_to_two_lines(escape_ass(c["text"]), wrap_width)
                override = f"{{{pos}\\fad(260,160)\\blur0.6\\fs{en_size}}}"
                start = cursor + c["start"]
                end = cursor + c["end"]
                events.append(
                    f"Dialogue: 0,{sec_to_ass(start)},{sec_to_ass(end)},"
                    f"English,,0,0,0,,{override + en_wrapped}"
                )

        log.info(
            "Ayah %d:%d -> %d Arabic reading groups, %d translation chunks "
            "(ar_fs=%s, en_fs=%s)",
            surah, ayah, len(ar_plan), len(en_plan), ar_size, en_size,
        )

        cursor += duration

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
        f.write("\n")

    log.info("Subtitle file written -> %s (%d ayat)", out_path.name, len(batch))
