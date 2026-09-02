#!/usr/bin/env python3
"""
surah_validator.py
Pre-upload validation gate (spec section 27). Every check must pass
before surah_uploader.py is ever invoked; on any failure this raises
ValidationError with a clear, specific reason — the caller (surah_builder.py)
catches it, logs it, and stops before upload. Nothing here uploads
anything.
"""

import json
import subprocess
from pathlib import Path

from config import LONGFORM_VIDEO_WIDTH, LONGFORM_VIDEO_HEIGHT, LONGFORM_VIDEO_FPS
from surah_timeline import SurahTimeline
from logging_utils import get_logger

log = get_logger(__name__)


class ValidationError(Exception):
    pass


def _ffprobe_json(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise ValidationError(f"ffprobe could not read {path.name}: {r.stderr[-300:]}")
    return json.loads(r.stdout)


def quick_media_check(path: Path, *, min_duration: float = 0.2,
                       require_video: bool = False, require_audio: bool = False,
                       expected_width: int = None, expected_height: int = None) -> bool:
    """
    Cheap, non-raising validity check for a CACHED artifact before trusting
    it and skipping regeneration (spec section 10: "do not treat 'file
    exists' as proof that an artifact is valid"). Returns False (never
    raises) for anything unreadable/corrupt/short/wrong-shaped, so every
    caller's pattern is simply:
        if path.exists() and not force and quick_media_check(path, ...):
            reuse
        else:
            regenerate
    This is deliberately looser than validate_final_video() (which is the
    hard pre-upload gate) — it only needs to catch "this cached file is
    unusable," not enforce every final-delivery requirement.
    """
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        info = _ffprobe_json(path)
    except ValidationError:
        return False

    try:
        duration = float(info["format"].get("duration", 0))
    except (KeyError, ValueError, TypeError):
        return False
    if duration < min_duration:
        return False

    v_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    a_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if require_video and not v_streams:
        return False
    if require_audio and not a_streams:
        return False
    if expected_width is not None or expected_height is not None:
        if not v_streams:
            return False
        w, h = int(v_streams[0].get("width", 0)), int(v_streams[0].get("height", 0))
        if expected_width is not None and w != expected_width:
            return False
        if expected_height is not None and h != expected_height:
            return False

    return True


def _mean_volume_db(path: Path) -> float:
    r = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    for line in r.stderr.splitlines():
        if "mean_volume" in line:
            try:
                return float(line.strip().split(":")[1].replace("dB", "").strip())
            except (IndexError, ValueError):
                pass
    raise ValidationError(f"Could not determine audio level for {path.name}")


def validate_final_video(video_path: Path, thumbnail_path: Path, metadata_path: Path,
                          timeline: SurahTimeline, expected_ayah_count: int,
                          min_duration: float, require_thumbnail: bool = True) -> None:
    """Raises ValidationError on the first failed check; logs and returns
    normally if every check passes.

    `require_thumbnail=False` (set by the builder when --skip-thumbnail
    was passed, or thumbnails are disabled in config) skips ONLY the
    thumbnail-presence check — every other check still runs. Without this,
    a deliberately-skipped thumbnail would fail validation for a reason
    the run was explicitly told not to care about."""

    # 1-2. Exists / readable.
    if not video_path.exists() or video_path.stat().st_size == 0:
        raise ValidationError(f"Video file missing or empty: {video_path}")
    info = _ffprobe_json(video_path)

    v_streams = [s for s in info["streams"] if s["codec_type"] == "video"]
    a_streams = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if not v_streams:
        raise ValidationError("No video stream found in output file.")
    if not a_streams:
        raise ValidationError("No audio stream found in output file.")
    vstream, astream = v_streams[0], a_streams[0]

    # 3. Resolution.
    width, height = int(vstream["width"]), int(vstream["height"])
    if (width, height) != (LONGFORM_VIDEO_WIDTH, LONGFORM_VIDEO_HEIGHT):
        raise ValidationError(
            f"Resolution is {width}x{height}, expected {LONGFORM_VIDEO_WIDTH}x{LONGFORM_VIDEO_HEIGHT}. "
            f"Refusing to upload a non-4K file."
        )

    # 4. Aspect ratio.
    if abs((width / height) - (16 / 9)) > 0.01:
        raise ValidationError(f"Aspect ratio {width}/{height} is not 16:9.")

    # 4b. Frame rate matches configured target.
    fps_str = vstream.get("r_frame_rate", "0/1")
    try:
        num, den = fps_str.split("/")
        fps = float(num) / float(den) if float(den) else 0
    except (ValueError, ZeroDivisionError):
        fps = 0
    if abs(fps - LONGFORM_VIDEO_FPS) > 0.05:
        raise ValidationError(f"Frame rate is {fps:.3f}fps, expected {LONGFORM_VIDEO_FPS}fps.")

    # 5. Duration (video vs expected minimum, and video vs audio agreement — check 7).
    duration = float(info["format"]["duration"])
    if duration < min_duration - 2.0:
        raise ValidationError(f"Video duration {duration:.1f}s is shorter than expected {min_duration:.1f}s.")

    audio_duration = float(astream.get("duration", duration))
    if abs(duration - audio_duration) > 3.0:
        raise ValidationError(
            f"Video/audio duration mismatch: video={duration:.1f}s audio={audio_duration:.1f}s."
        )

    # 6. Audio exists (redundant with #4 above but explicit per spec list) — already confirmed.

    # 8. Audio is not silent.
    mean_db = _mean_volume_db(video_path)
    if mean_db < -50.0:
        raise ValidationError(f"Audio appears silent (mean volume {mean_db:.1f} dB).")

    # 9. Video contains frames throughout — nb_frames sanity vs duration*fps.
    nb_frames = int(vstream.get("nb_frames", 0) or 0)
    if fps > 0 and nb_frames > 0:
        expected_frames = duration * fps
        if nb_frames < expected_frames * 0.9:
            raise ValidationError(
                f"Frame count {nb_frames} is well below expected ~{expected_frames:.0f} "
                f"for {duration:.1f}s at {fps:.2f}fps — encode may have truncated."
            )

    # 10. No obvious ffmpeg encoding failure — codec sanity.
    if vstream.get("codec_name") != "h264":
        raise ValidationError(f"Video codec is {vstream.get('codec_name')}, expected h264.")
    if astream.get("codec_name") != "aac":
        raise ValidationError(f"Audio codec is {astream.get('codec_name')}, expected aac.")

    # 11. Subtitle timeline valid (already validated when built — re-check shape here).
    if not timeline.entries:
        raise ValidationError("Subtitle timeline is empty.")

    # 12. Expected verse count present.
    if len(timeline.entries) != expected_ayah_count:
        raise ValidationError(
            f"Timeline has {len(timeline.entries)} ayat, expected {expected_ayah_count}."
        )

    # 13. Thumbnail exists (only when the run actually required one).
    if require_thumbnail:
        if not thumbnail_path.exists() or thumbnail_path.stat().st_size == 0:
            raise ValidationError(f"Thumbnail missing or empty: {thumbnail_path}")

    # 14. Metadata exists.
    if not metadata_path.exists():
        raise ValidationError(f"Metadata file missing: {metadata_path}")
    try:
        meta = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValidationError(f"Metadata file is not valid JSON: {e}")
    for field in ("title", "description", "tags", "chapters"):
        if not meta.get(field):
            raise ValidationError(f"Metadata missing required field: {field}")

    log.info(
        "Validation passed: %dx%d @ %.2ffps, %.1fs, %s/%s, %d ayat, audio %.1fdB.",
        width, height, fps, duration, vstream.get("codec_name"), astream.get("codec_name"),
        len(timeline.entries), mean_db,
    )
