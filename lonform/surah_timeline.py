#!/usr/bin/env python3
"""
surah_timeline.py
Builds the authoritative per-ayah timeline for a full-Surah render:
surah/ayah number, Arabic text, English translation, and real
start/end timestamps derived directly from the actual decoded ayah
audio durations (never estimated/assumed equal-length — see
surah_audio.download_full_surah for how those durations are measured).

RECITATION-RELATIVE, ALWAYS. Every entry's start/end is timed from
ayah 1 = the beginning of the recitation audio (t=0), because that is
also the clock of every file this timeline is used against: the
mastered audio, the background video, and the ASS subtitle track are
all built and burned in BEFORE the intro is prepended, so all of them
share the same t=0-at-recitation-start clock. The intro shifts
everything in the FINAL assembled video by its own rendered duration —
correctly, automatically, and without a single offset needing to be
threaded through this module — simply because it is concatenated in
front of that already-correct block (see surah_renderer.join_segments).
This timeline is never built with a nonzero offset baked into its
entries; a caller that needs a position in the FINAL video (YouTube
chapter timestamps) asks for that explicitly via chapter_points(offset=...)
without touching the entries themselves, so the one place that legitimately
needs the intro's duration (chapter timestamps in the description) can't
leak into the burned-in subtitle timing (which must never see it).

This timeline is the single source of truth consumed by
surah_subtitles.py (subtitle timing), surah_metadata_gen.py (YouTube
chapters) and surah_validator.py (verse-count / duration cross-check) —
so subtitles, chapters, and validation can never drift out of sync with
each other or with the actual audio.
"""

from dataclasses import dataclass, field

from subtitle_builder import get_ayah_text
from logging_utils import get_logger

log = get_logger(__name__)


@dataclass
class AyahEntry:
    surah: int
    ayah: int
    start: float
    end: float
    duration: float
    arabic: str
    english: str


@dataclass
class SurahTimeline:
    surah_num: int
    entries: list = field(default_factory=list)

    @property
    def total_duration(self) -> float:
        return self.entries[-1].end if self.entries else 0.0

    def chapter_points(self, offset: float = 0.0) -> list:
        """[(start_seconds, label), ...] — one per ayah, in order.
        `offset` (typically the rendered intro's duration) shifts the
        returned timestamps to describe positions in the FINAL assembled
        video; it never touches the entries themselves, which stay
        recitation-relative for subtitle burning."""
        return [(e.start + offset, f"Ayah {e.ayah}") for e in self.entries]


def build_timeline(surah_num: int, ayah_durations: list, arabic_data, english_data) -> SurahTimeline:
    """
    ayah_durations: ordered list of per-ayah durations (seconds), ayah 1..N,
    exactly as measured from the decoded audio files (see
    surah_audio.download_full_surah). Always recitation-relative — ayah 1
    starts at t=0 — see module docstring for why no intro offset belongs
    here.
    """
    entries = []
    cursor = 0.0
    for ayah_num, duration in enumerate(ayah_durations, start=1):
        ar_text = get_ayah_text(arabic_data, surah_num, ayah_num)
        en_text = get_ayah_text(english_data, surah_num, ayah_num)
        start, end = cursor, cursor + duration
        entries.append(AyahEntry(
            surah=surah_num, ayah=ayah_num, start=start, end=end,
            duration=duration, arabic=ar_text, english=en_text,
        ))
        cursor = end

    timeline = SurahTimeline(surah_num=surah_num, entries=entries)
    log.info("Timeline built: Surah %d, %d ayat, recitation span %.2fs (recitation-relative, t=0 at Ayah 1).",
              surah_num, len(entries), sum(ayah_durations))
    return timeline


def validate_timeline(timeline: SurahTimeline, expected_ayah_count: int,
                       audio_duration: float, tolerance: float = 1.5) -> None:
    """
    Spec section 6: total verse timing must approximately match the final
    audio duration, and every expected verse must be present. Raises
    RuntimeError (never silently continues) on any mismatch.
    """
    if len(timeline.entries) != expected_ayah_count:
        raise RuntimeError(
            f"Timeline has {len(timeline.entries)} ayat, expected {expected_ayah_count} "
            f"for Surah {timeline.surah_num}."
        )
    missing_text = [
        f"{e.surah}:{e.ayah}" for e in timeline.entries if not e.arabic or not e.english
    ]
    if missing_text:
        raise RuntimeError(f"Missing Arabic/English text for: {', '.join(missing_text)}")

    if abs(timeline.entries[0].start) > 0.001:
        raise RuntimeError(
            f"Timeline is not recitation-relative: Ayah 1 starts at "
            f"{timeline.entries[0].start:.3f}s, expected 0.0s."
        )

    recitation_span = timeline.entries[-1].end - timeline.entries[0].start
    if abs(recitation_span - audio_duration) > tolerance:
        raise RuntimeError(
            f"Timeline span {recitation_span:.2f}s does not match audio duration "
            f"{audio_duration:.2f}s (tolerance {tolerance}s) for Surah {timeline.surah_num}."
        )
    log.info("Timeline validated: %d/%d ayat, recitation-relative, span matches audio within %.2fs.",
              len(timeline.entries), expected_ayah_count, tolerance)
