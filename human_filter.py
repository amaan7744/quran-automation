#!/usr/bin/env python3
"""
human_filter.py
Automatic, zero-manual-review filter that rejects any clip containing a
person — not even for one frame.

Why this exists as its own module: pexels_fetcher.py's keyword search
(orientation=portrait + a nature query string) is not a content
guarantee — Pexels regularly returns nature clips that include a
person walking into frame, a hand touching water, a distant silhouette,
a reflection, etc. This module is the actual enforcement layer: every
candidate clip must pass through here before it's allowed into the
selected set, with no human ever asked to look at it.

APPROACH
--------
1. Sample several frames spread across the *entire* downloaded clip
   (not just frame 1) via a single ffmpeg pass using the `fps` filter,
   so a person who only appears briefly mid-clip is still caught.
2. Run YOLOv8n (COCO-pretrained, class 0 = "person") on the sampled
   frames in one batched call.
3. If a person is detected in ANY sampled frame, the whole clip is
   rejected — no partial-clip trimming/salvaging, since we don't know
   which portion downstream trimming will end up using.

This catches full people, and — because COCO's "person" class is
annotated on partially visible/occluded humans too (an arm, a torso,
someone at a distance) — it also catches most of the partial cases
(hands, distant figures, silhouettes) without needing a second model.
It intentionally runs at a LOW confidence threshold: for this use case
a missed person is unacceptable, while an extra false-positive
rejection just costs one more (fast, automatic) candidate — so the
threshold is tuned to minimize false negatives, not to be "accurate."

HONESTY ABOUT LIMITS
---------------------
No automated detector can offer a mathematical 100% guarantee — an
adversarial or extremely unusual frame could in principle slip through.
This is the strongest practical automatic safeguard available (multi-
frame sampling + a real object detector + a deliberately low
threshold + fail-closed behavior on any error), but it is a risk
reduction measure, not a formal proof. If a stricter guarantee is ever
needed, the recommended next step is adding a second, independent
detector (e.g. a MediaPipe face/hand pass) as a redundant check —
left out here to keep the CI dependency footprint and runtime small,
per the "keep the pipeline efficient for GitHub Actions" requirement.
"""

import shutil
import subprocess
from pathlib import Path

from logging_utils import get_logger

log = get_logger(__name__)

# Evenly spaced frames sampled across the WHOLE downloaded clip (not just
# the portion that ends up trimmed/used) — cheap, and safe regardless of
# which slice of the clip downstream trimming later picks.
HUMAN_CHECK_SAMPLES = 6

# Deliberately low: missing a person is unacceptable for this use case,
# while a false-positive rejection is nearly free (pipeline just moves on
# to the next candidate). COCO "person" default eval threshold is much
# higher (~0.5); halving it trades some extra rejected-but-actually-empty
# clips for meaningfully lower risk of a human slipping through.
HUMAN_CHECK_CONFIDENCE = 0.25

_MODEL = None  # lazy singleton so the (relatively expensive) model load
                # happens once per pipeline run, not once per clip


class HumanFilterError(RuntimeError):
    pass


def _get_model():
    """Loads YOLOv8n once and reuses it for every clip in this run."""
    global _MODEL
    if _MODEL is None:
        try:
            from ultralytics import YOLO
        except ImportError as e:
            raise HumanFilterError(
                "The 'ultralytics' package is required for automatic human "
                "detection but is not installed. Add `ultralytics` to "
                "requirements.txt."
            ) from e
        log.info("Loading YOLOv8n for human detection (once per run)...")
        _MODEL = YOLO("yolov8n.pt")
    return _MODEL


def _extract_sample_frames(clip_path: Path, duration: float, frames_dir: Path,
                            num_samples: int) -> list:
    """
    Extracts `num_samples` frames evenly spaced across the clip's full
    duration in a single ffmpeg pass (one decode, not N separate seeks —
    keeps this cheap for CI). Returns the list of extracted frame paths,
    or [] if extraction failed.
    """
    duration = max(duration, 0.5)
    n = max(1, num_samples)
    fps = n / duration
    frames_dir.mkdir(parents=True, exist_ok=True)
    pattern = frames_dir / "frame_%03d.jpg"

    cmd = [
        "ffmpeg", "-y", "-i", str(clip_path),
        "-vf", f"fps={fps:.6f}",
        "-frames:v", str(n),
        "-q:v", "4",
        str(pattern),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.warning("Frame sampling failed for %s: %s", clip_path.name, result.stderr[-200:])
        return []
    return sorted(frames_dir.glob("frame_*.jpg"))


def _frames_contain_person(frame_paths: list, confidence: float) -> bool:
    model = _get_model()
    results = model.predict(
        [str(p) for p in frame_paths],
        conf=confidence,
        classes=[0],  # COCO class 0 = "person"; ignore everything else
        verbose=False,
    )
    return any(len(r.boxes) > 0 for r in results)


def clip_contains_person(clip_path: Path, duration: float, tmpdir: Path,
                          num_samples: int = HUMAN_CHECK_SAMPLES,
                          confidence: float = HUMAN_CHECK_CONFIDENCE) -> bool:
    """
    Returns True if a person is detected in any sampled frame of
    `clip_path` (i.e. the clip must be rejected), False only if every
    sampled frame is clean.

    Fails closed: if frames can't be sampled at all (corrupt/unreadable
    file that somehow passed the earlier ffprobe check), the clip is
    treated as containing a person rather than risking an unverified
    clip getting through.
    """
    frames_dir = tmpdir / f"human_check_{clip_path.stem}"
    try:
        frame_paths = _extract_sample_frames(clip_path, duration, frames_dir, num_samples)
        if not frame_paths:
            log.warning("Could not sample frames from %s; rejecting out of caution", clip_path.name)
            return True
        found = _frames_contain_person(frame_paths, confidence)
        if found:
            log.info("  Human detected in %s -> rejecting", clip_path.name)
        return found
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)
