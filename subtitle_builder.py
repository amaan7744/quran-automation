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

Sizing is still driven by the *full ayah's* character length (not just a
chunk's), so a given ayah keeps one consistent, confident size across all
of its reading chunks — short ayat run large and bold, long ones settle
to a smaller (but still enlarged, see ARABIC_MIN/MAX_FONT_SIZE) size.
Because each individual chunk is only ~3-4 words, the two-line cap is
essentially never a binding constraint in practice; a capacity safety net
still exists for the rare case of unusually long words.

ARABIC KARAOKE DIRECTION
-------------------------
See `_rtl_karaoke_wrap` for a full explanation of why plain `{\\kNN}word`
sequences visually highlight left-to-right (like English) even though the
Arabic text itself is stored in correct reading order, and why the fix is
to decouple screen position (controlled by word order) from highlight
timing (controlled by explicit `\\t` color transforms), rather than
relying on Unicode bidi control characters, which do not affect libass's
run-by-run placement of `\\k`-separated text.

Each word cycles through three colors: soft white while upcoming, a brief
emerald flash the instant it's recited, then settles to soft grey to read
as "already recited" — a subtle three-stage transform rather than a
binary on/off highlight.
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
# Sizes increased ~20% over the previous 46/110 floor/ceiling so Arabic
# reads as the clear visual focus of the frame, as on premium Quran reels.
ARABIC_MIN_FONT_SIZE = 56   # floor for legibility after social-platform re-encoding
ARABIC_MAX_FONT_SIZE = 132  # ceiling so very short ayat don't look oversized
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


def adaptive_arabic_size(text: str) -> int:
    """
    Picks the largest font size (within [MIN, MAX]) at which the *full
    ayah* `text` would fit within ARABIC_MAX_LINES lines. This gives each
    ayah one consistent, confident size for all of its reading chunks —
    short ayat get a large, bold size and long ones shrink gradually
    instead of jumping between a few fixed presets.
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
# capacity safety-net for translation chunks that run long relative to
# their aligned Arabic reading group.
EN_PX_PER_CHAR_PER_FONTSIZE = 15.5 / 56.0
EN_MARGIN_L = 90
EN_MARGIN_R = 90
EN_USABLE_WIDTH = PLAY_RES_X - EN_MARGIN_L - EN_MARGIN_R
EN_MAX_LINES = 2
EN_WRAP_SAFETY = 0.90
# Sizes increased so the translation is comfortably readable while
# remaining clearly secondary to the (larger) Arabic above it.
EN_MIN_FONT_SIZE = 42  # floor — never too small to read on a phone
EN_MAX_FONT_SIZE = 64  # ceiling for short translations


def english_line_capacity(font_size: int, lines: int = EN_MAX_LINES) -> int:
    px_per_char = EN_PX_PER_CHAR_PER_FONTSIZE * font_size
    total_px = lines * EN_USABLE_WIDTH * EN_WRAP_SAFETY
    return max(1, int(total_px / px_per_char))


def adaptive_english_size(text: str) -> int:
    """
    Smoothly scales translation font size between EN_MIN_FONT_SIZE and
    EN_MAX_FONT_SIZE based on the full ayah's translation length, the
    same closed-form approach used for Arabic — short ayat get a large,
    confident size and long ones shrink gradually instead of jumping
    between a few fixed buckets.
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


# ── RTL karaoke ─────────────────────────────────────────────────────────
# Colors used for the manual per-word highlight transform below. These must
# stay in sync with the "Arabic" style's PrimaryColour / SecondaryColour in
# HEADER_TEMPLATE since we don't let \k drive the Primary/Secondary swap —
# we set colors explicitly per word across three stages instead:
#   upcoming (not yet recited)  -> soft white
#   active   (being recited)    -> emerald
#   read     (already recited)  -> soft grey
ARABIC_KARAOKE_UPCOMING_COLOR = "&H00F5F5F5&"  # soft white — not yet recited
ARABIC_KARAOKE_ACTIVE_COLOR   = "&H0081B910&"  # emerald — currently being recited
ARABIC_KARAOKE_READ_COLOR     = "&H00C9C9C9&"  # soft grey — already recited

# Length of the color transforms themselves (ms). Short enough to read as
# a gentle "flash then settle" per word rather than a visible wipe, but
# non-zero so libass has a well-formed \t interval to animate.
KARAOKE_ACTIVE_TRANSFORM_MS = 90   # white -> emerald, on recitation
KARAOKE_READ_TRANSFORM_MS = 160    # emerald -> grey, settling to "read"


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
      - Highlight timing is controlled by explicit `\\t(t1,t2,\\cXXXXXX)`
        color transforms per word, using absolute millisecond offsets
        relative to this Dialogue line's own start time — not `\\k`'s
        cumulative, order-dependent duration. Word 1 always gets t1=0
        regardless of where it physically sits in the string, so its
        highlight fires first no matter its screen position.

    Each word carries two chained transforms rather than one: it starts
    soft white, flashes emerald the instant it's recited, then settles to
    soft grey shortly after — reading clearly as "already recited" while
    the next word (still white) awaits its own turn.

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
        active_end_ms = start_ms + min(duration_ms, KARAOKE_ACTIVE_TRANSFORM_MS)
        read_end_ms = active_end_ms + KARAOKE_READ_TRANSFORM_MS
        timed_words.append((word, start_ms, active_end_ms, read_end_ms))
        cumulative_ms += duration_ms

    # Only NOW reverse for display: last-recited word first in the string,
    # first-recited word last — so word 1 ends up rightmost on screen.
    # Its timing windows travel with it, so timing is unaffected.
    parts = [
        f"{{\\c{ARABIC_KARAOKE_UPCOMING_COLOR}"
        f"\\t({start_ms},{active_end_ms},\\c{ARABIC_KARAOKE_ACTIVE_COLOR})"
        f"\\t({active_end_ms},{read_end_ms},\\c{ARABIC_KARAOKE_READ_COLOR})}}"
        f"{escape_ass(word)}"
        for word, start_ms, active_end_ms, read_end_ms in reversed(timed_words)
    ]
    return " ".join(parts)


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
    """Returns a list of dicts: {start, end, word_timings, lines} — one
    per Arabic reading group, times relative to the ayah's own start."""
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
            "word_timings": word_timings[i0:i1],
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
#   Arabic upcoming color  = soft white  (F5F5F5) — words not yet recited.
#   Arabic active color    = emerald     (10B981) — the word being recited
#     right now, held briefly before settling.
#   Arabic read color      = soft grey   (C9C9C9) — words already recited.
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
Style: Arabic,{ar_font},132,&H00F5F5F5,&H00C9C9C9,&H00141414,&H50000000,0,0,0,0,100,100,0,0,1,4,2,8,80,80,140,1
Style: English,{en_font},64,&H00EDEDED,&H00EDEDED,&H00141414,&H60000000,0,0,0,0,100,100,0,0,1,3,1,8,90,90,170,1

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
        with right-to-left karaoke word-highlighting (soft white ->
        emerald -> soft grey) and a gentle fade + scale animation.
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
        en_size = adaptive_english_size(en_text) if en_text else None

        ar_plan = plan_arabic_chunks(ar_text, duration, ar_size) if ar_text else []
        ar_windows = [(c["start"], c["end"]) for c in ar_plan]
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
                karaoke_body = _rtl_karaoke_wrap(c["word_timings"])
                override = (
                    f"{{{pos}\\fad(260,160)\\fscx97\\fscy97"
                    f"\\t(0,200,2,\\fscx100\\fscy100)\\blur2\\fs{ar_size}}}"
                )
                start = cursor + c["start"]
                end = cursor + c["end"]
                events.append(
                    f"Dialogue: 1,{sec_to_ass(start)},{sec_to_ass(end)},"
                    f"Arabic,,0,0,0,,{override + karaoke_body}"
                )

        if en_plan:
            pos = f"\\an8\\pos({PLAY_RES_X // 2},{en_top})"
            wrap_width = max(int(2600 / en_size), 18)
            for c in en_plan:
                en_wrapped = wrap_to_two_lines(escape_ass(c["text"]), wrap_width)
                override = (
                    f"{{{pos}\\fad(260,160)\\fscx98\\fscy98"
                    f"\\t(0,200,2,\\fscx100\\fscy100)\\blur1\\fs{en_size}}}"
                )
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
