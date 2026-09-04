#!/usr/bin/env python3
"""
surah_backgrounds.py
Landscape (16:9) cinematic background assembly for the long-form
pipeline. Reuses pexels_fetcher.py's search/download/quality/human-filter/
trim/crossfade machinery end-to-end via the orientation/dimension/cache
parameters added to it for this feature (see pexels_fetcher.collect_clips
/ build_background) — nothing here re-implements Pexels search, clip
validation, or the human/vehicle content filter.

SCALABILITY NOTE (spec sections 10-12, 22):
A single ffmpeg xfade filter graph with one input per clip does not scale
to an hours-long Surah — a 3-hour video at ~16s/clip needs ~650+
simultaneous ffmpeg inputs, which is exactly the kind of giant
intermediate-file/unbounded-memory pattern spec section 22 rules out.
Instead this module builds the background in two levels, reusing
pexels_fetcher.crossfade_concat at BOTH levels:
  1. Clips are gathered in batches of LONGFORM_SEGMENT_CLIP_COUNT and each
     batch is crossfade-concatenated into one intermediate "segment" file
     (a few minutes long).
  2. The segment files themselves are crossfade-concatenated together the
     same way, so the transition AT a segment boundary is just as smooth
     as every other transition — there is no hard cut anywhere, and at no
     point does ffmpeg see more than ~24 simultaneous inputs.
"""

import subprocess
from pathlib import Path

from pexels_fetcher import collect_clips, trim_and_normalize, crossfade_concat, PexelsError
from longform_config import (
    LONGFORM_VIDEO_WIDTH, LONGFORM_VIDEO_HEIGHT, LONGFORM_VIDEO_FPS,
    LONGFORM_TRANSITION_DURATION, LONGFORM_DURATION_BUFFER,
    LONGFORM_CLIP_TRIM_MIN, LONGFORM_CLIP_TRIM_MAX,
    LONGFORM_SOURCE_CLIP_MIN_DURATION, LONGFORM_SOURCE_CLIP_MAX_DURATION,
    MIN_LONGFORM_CLIP_WIDTH, MIN_LONGFORM_CLIP_HEIGHT,
    NATIVE_4K_CLIP_WIDTH, NATIVE_4K_CLIP_HEIGHT,
    LONGFORM_QUERIES_PER_RUN, LONGFORM_CLIPS_PER_QUERY, LONGFORM_MAX_GATHER_ROUNDS,
    LONGFORM_SEGMENT_CLIP_COUNT, LONGFORM_CACHE_DIR, LONGFORM_CACHE_INDEX_FILE,
)
from surah_validator import quick_media_check
from logging_utils import get_logger

log = get_logger(__name__)


def _probe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    return float(r.stdout.strip())


def _crossfade_aware_duration_buffer() -> float:
    """
    The flat LONGFORM_DURATION_BUFFER config value is a starting point,
    not the actual multiplier collect_clips needs. Every crossfade
    transition between two clips SHRINKS the assembled total by
    `transition` seconds relative to simply summing clip durations — and
    this pipeline crossfades at BOTH levels (within each segment, and
    between segments), which together amount to exactly one crossfade per
    adjacent clip pair regardless of how they're batched (N clips in a
    chain always has N-1 transitions, however that chain is split into
    segments). So the sum of raw trim durations collect_clips gathers
    needs to exceed total_duration by roughly the fraction of an average
    clip that gets eaten by its transition — NOT by an arbitrary flat
    percentage that happens to work for a specific trim/transition
    combination and silently stops being enough if either is tuned later.

    This computes that ratio from the actual configured trim/transition
    values, then adds LONGFORM_DURATION_BUFFER on top as a safety margin
    for the randomness in per-clip trim length selection and the
    human/quality-filter rejection rate. The hard post-hoc check in
    build_landscape_background (joined_duration < total_duration) remains
    as the real guarantee either way — this just makes hitting it on the
    first attempt the common case instead of the exception.
    """
    avg_trim = (LONGFORM_CLIP_TRIM_MIN + LONGFORM_CLIP_TRIM_MAX) / 2
    net_per_clip = max(avg_trim - LONGFORM_TRANSITION_DURATION, 1.0)
    crossfade_loss_ratio = avg_trim / net_per_clip
    return crossfade_loss_ratio * LONGFORM_DURATION_BUFFER


def _collect_kwargs() -> dict:
    return dict(
        orientation="landscape",
        min_width=MIN_LONGFORM_CLIP_WIDTH,
        min_height=MIN_LONGFORM_CLIP_HEIGHT,
        min_duration=LONGFORM_SOURCE_CLIP_MIN_DURATION,
        max_duration=LONGFORM_SOURCE_CLIP_MAX_DURATION,
        trim_min=LONGFORM_CLIP_TRIM_MIN,
        trim_max=LONGFORM_CLIP_TRIM_MAX,
        duration_buffer=_crossfade_aware_duration_buffer(),
        queries_per_run=LONGFORM_QUERIES_PER_RUN,
        clips_per_query=LONGFORM_CLIPS_PER_QUERY,
        max_gather_rounds=LONGFORM_MAX_GATHER_ROUNDS,
        cache_dir=LONGFORM_CACHE_DIR,
        index_file=LONGFORM_CACHE_INDEX_FILE,
        # Spec section 3: prefer native 4K, then resolution, then source
        # diversity — long-form-only, does not touch Shorts/Reels' random
        # selection (prioritize_quality defaults to False there).
        prioritize_quality=True,
        native_width=NATIVE_4K_CLIP_WIDTH,
        native_height=NATIVE_4K_CLIP_HEIGHT,
        max_clips_per_source=3,
    )


def _log_resolution_tier(clip_path: Path) -> None:
    """
    Logs whether a source clip is native >=4K or will be upscaled from a
    lower (down to the MIN_LONGFORM_CLIP_* floor) resolution — spec
    section 3: "never silently accept poor-quality footage." This is
    purely a visibility log; trim_and_normalize's scale+crop already
    enforces the final frame is exactly LONGFORM_VIDEO_WIDTH x HEIGHT
    either way, and quality_filter.validate_clip already hard-rejects
    anything below the configured floor before this is ever called.
    """
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(clip_path)],
        capture_output=True, text=True,
    )
    try:
        w, h = (int(x) for x in r.stdout.strip().split("x"))
    except (ValueError, AttributeError):
        log.warning("  Could not determine source resolution for %s", clip_path.name)
        return
    if w >= NATIVE_4K_CLIP_WIDTH and h >= NATIVE_4K_CLIP_HEIGHT:
        log.info("  %s: native 4K source (%dx%d)", clip_path.name, w, h)
    else:
        log.warning("  %s: sub-4K source (%dx%d) — will be upscaled to %dx%d, "
                    "not a native-4K frame", clip_path.name, w, h,
                    LONGFORM_VIDEO_WIDTH, LONGFORM_VIDEO_HEIGHT)


def _build_segment(clips: list, tmpdir: Path, segment_out: Path) -> float:
    """Trims/normalizes + crossfade-concatenates one batch of clips into a
    single segment file. Returns the segment's actual rendered duration."""
    trimmed_paths, trimmed_durations = [], []
    for i, (path, duration) in enumerate(clips):
        _log_resolution_tier(path)
        trimmed_out = tmpdir / f"trim_{i:04d}.mp4"
        trim_and_normalize(
            path, trimmed_out, duration,
            width=LONGFORM_VIDEO_WIDTH, height=LONGFORM_VIDEO_HEIGHT, fps=LONGFORM_VIDEO_FPS,
        )
        trimmed_paths.append(trimmed_out)
        trimmed_durations.append(duration)

    crossfade_concat(
        trimmed_paths, trimmed_durations, segment_out,
        transition=LONGFORM_TRANSITION_DURATION, fps=LONGFORM_VIDEO_FPS,
    )
    return _probe_duration(segment_out)


def build_landscape_background(total_duration: float, work_dir: Path, out_path: Path,
                                force: bool = False) -> None:
    """
    Full long-form background pipeline: gather enough landscape clips to
    cover total_duration (with a crossfade-loss-aware buffer — see
    _crossfade_aware_duration_buffer), assemble them into segments, then
    crossfade the segments together into one continuous background of at
    least total_duration seconds.

    Cached: if out_path already exists, is ffprobe-valid, matches the
    configured resolution, and is long enough, it's reused as-is
    (stage-resume — spec section 23). An existing-but-invalid or
    wrong-resolution file is never trusted just because it exists.
    """
    if out_path.exists() and not force:
        if quick_media_check(out_path, min_duration=total_duration - 1.0, require_video=True,
                              expected_width=LONGFORM_VIDEO_WIDTH, expected_height=LONGFORM_VIDEO_HEIGHT):
            log.info("Reusing existing background -> %s", out_path.name)
            return
        log.warning("Existing background at %s failed validity/length/resolution check — rebuilding.",
                    out_path.name)

    bg_dir = work_dir / "backgrounds"
    bg_dir.mkdir(parents=True, exist_ok=True)
    segments_dir = bg_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = bg_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    try:
        clips = collect_clips(total_duration, raw_dir, **_collect_kwargs())
    except PexelsError as e:
        raise PexelsError(f"Long-form background collection failed: {e}") from e

    batches = [
        clips[i:i + LONGFORM_SEGMENT_CLIP_COUNT]
        for i in range(0, len(clips), LONGFORM_SEGMENT_CLIP_COUNT)
    ]
    log.info("Assembling background from %d clips across %d segment(s)...", len(clips), len(batches))

    segment_paths, segment_durations = [], []
    for i, batch in enumerate(batches):
        seg_out = segments_dir / f"segment_{i:03d}.mp4"
        reuse_segment = (
            seg_out.exists() and not force and
            quick_media_check(seg_out, min_duration=0.5, require_video=True,
                               expected_width=LONGFORM_VIDEO_WIDTH, expected_height=LONGFORM_VIDEO_HEIGHT)
        )
        if reuse_segment:
            seg_dur = _probe_duration(seg_out)
        else:
            seg_tmp = segments_dir / f"work_{i:03d}"
            seg_tmp.mkdir(parents=True, exist_ok=True)
            seg_dur = _build_segment(batch, seg_tmp, seg_out)
        segment_paths.append(seg_out)
        segment_durations.append(seg_dur)
        log.info("  Segment %d/%d ready: %.1fs (%d clips)", i + 1, len(batches), seg_dur, len(batch))

    # Level 2: crossfade the segment files together, exactly like level 1
    # crossfades raw clips — same function, same "never a hard cut" result
    # at every segment boundary too.
    joined = bg_dir / "bg_full.mp4"
    crossfade_concat(
        segment_paths, segment_durations, joined,
        transition=LONGFORM_TRANSITION_DURATION, fps=LONGFORM_VIDEO_FPS,
    )

    joined_duration = _probe_duration(joined)
    if joined_duration < total_duration:
        raise PexelsError(
            f"Assembled background is {joined_duration:.1f}s, short of the "
            f"{total_duration:.1f}s needed — Pexels didn't yield enough usable footage."
        )

    # Exact-length trim so downstream compositing never has to guess.
    # This is an INTERMEDIATE file: render_main_body() (surah_renderer.py)
    # re-encodes it again at LONGFORM_VIDEO_CRF/slow when muxing in audio
    # and burning subtitles. Encoding this intermediate at crf=16/medium
    # (near-lossless, slow) was wasted work — a full multi-hour re-encode
    # pass whose extra quality is immediately thrown away by the next
    # re-encode. crf=18/veryfast matches the quality margin actually used
    # downstream while cutting this pass's render time substantially; the
    # final output's real quality gate is still LONGFORM_VIDEO_CRF in
    # render_main_body, unchanged.
    cmd = [
        "ffmpeg", "-y", "-i", str(joined), "-t", str(total_duration),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-an", str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Final background trim failed: {result.stderr[-500:]}")

    log.info("Long-form background ready -> %s (%.1fs, %d clips, %d segments)",
              out_path.name, total_duration, len(clips), len(batches))
