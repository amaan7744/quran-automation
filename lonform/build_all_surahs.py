#!/usr/bin/env python3
"""
build_all_surahs.py
Batch driver over all 114 Surahs, using the exact same build_surah()
pipeline/resume behavior as build_surah.py — one Surah's failure does not
stop the rest of the run; every failure is collected and reported at the
end so a single bad run can be diagnosed and retried (via the normal
resume mechanism) without redoing everything that already succeeded.

Usage:
    python build_all_surahs.py
    python build_all_surahs.py --start 1 --end 20 --no-upload
    python build_all_surahs.py --surahs 1,36,55,67,114
"""

import argparse
import sys

from surah_builder import build_surah, BuildError
from quran_metadata import SurahNotFoundError, all_surah_numbers
from logging_utils import get_logger

log = get_logger(__name__)


def _parse_surah_list(raw: str) -> list:
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch-build long-form videos for multiple Surahs.")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=114)
    parser.add_argument("--surahs", type=str, default=None,
                         help="Comma-separated explicit list, e.g. 1,36,55,67,114 (overrides --start/--end).")
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-background-download", action="store_true")
    parser.add_argument("--skip-thumbnail", action="store_true")
    args = parser.parse_args()

    if args.surahs:
        surah_numbers = _parse_surah_list(args.surahs)
    else:
        surah_numbers = [n for n in all_surah_numbers() if args.start <= n <= args.end]

    privacy = "private" if args.private else None
    succeeded, failed = [], []

    for n in surah_numbers:
        log.info("=" * 70)
        log.info("Surah %d / batch of %d", n, len(surah_numbers))
        log.info("=" * 70)
        try:
            build_surah(
                n,
                upload=(False if args.no_upload else None),
                privacy=privacy,
                force=args.force,
                skip_background_download=args.skip_background_download,
                skip_thumbnail=args.skip_thumbnail,
            )
            succeeded.append(n)
        except (BuildError, SurahNotFoundError) as e:
            log.error("Surah %d failed: %s", n, e)
            failed.append((n, str(e)))

    log.info("=" * 70)
    log.info("Batch complete: %d succeeded, %d failed.", len(succeeded), len(failed))
    if failed:
        for n, reason in failed:
            log.error("  Surah %d: %s", n, reason)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
