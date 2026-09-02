#!/usr/bin/env python3
"""
build_surah.py
CLI for the long-form Full-Surah pipeline.

Usage:
    python build_surah.py --surah 67
    python build_surah.py --surah 67 --no-upload
    python build_surah.py --surah 67 --private
    python build_surah.py --surah 67 --force
    python build_surah.py --surah 67 --skip-background-download
    python build_surah.py --surah 67 --skip-thumbnail
"""

import argparse
import sys

from surah_builder import build_surah, BuildError
from quran_metadata import SurahNotFoundError
from logging_utils import get_logger

log = get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a full-Surah long-form YouTube video.")
    parser.add_argument("--surah", type=int, required=True, help="Surah number (1-114).")
    parser.add_argument("--no-upload", action="store_true", help="Render only; do not upload to YouTube.")
    parser.add_argument("--private", action="store_true", help="Upload as private (overrides LONGFORM_UPLOAD_PRIVACY).")
    parser.add_argument("--force", action="store_true", help="Ignore cached/completed stages and rebuild everything.")
    parser.add_argument("--skip-background-download", action="store_true",
                         help="Reuse an already-built background.mp4 for this Surah; fail if none exists.")
    parser.add_argument("--skip-thumbnail", action="store_true", help="Skip thumbnail generation.")
    args = parser.parse_args()

    privacy = "private" if args.private else None

    try:
        video_path = build_surah(
            args.surah,
            upload=(False if args.no_upload else None),
            privacy=privacy,
            force=args.force,
            skip_background_download=args.skip_background_download,
            skip_thumbnail=args.skip_thumbnail,
        )
    except (BuildError, SurahNotFoundError) as e:
        log.error("Build failed: %s", e)
        return 1

    log.info("Done: %s", video_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
