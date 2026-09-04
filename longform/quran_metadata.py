#!/usr/bin/env python3
"""
quran_metadata.py
Dynamic Surah metadata for the long-form pipeline — never hard-codes a
single Surah. Verse count / English / Arabic names reuse surah_data.SURAHS
(the same source build_video.py and audio_downloader.py already trust).
Revelation type (Meccan/Medinan) is NOT present in surah_data.py, so it is
fetched once from a public Quran API and cached to disk — every subsequent
call/run for that Surah is free and works fully offline.
"""

import json
import time
from pathlib import Path

import requests

from surah_data import SURAHS
from longform_config import LONGFORM_META_CACHE_FILE
from logging_utils import get_logger

log = get_logger(__name__)

SURAH_MAP = {s[0]: s for s in SURAHS}

# api.alquran.cloud is a free, no-key public Quran API. Used ONLY for the
# one field surah_data.py doesn't carry (revelation type). If it's ever
# unreachable (offline dev box, firewalled CI runner, API downtime), we
# degrade to "Unknown" rather than fail the whole build over one metadata
# field — see get_revelation_type().
REVELATION_API_URL = "https://api.alquran.cloud/v1/surah/{n}"


class SurahNotFoundError(ValueError):
    pass


def _load_cache() -> dict:
    if LONGFORM_META_CACHE_FILE.exists():
        try:
            return json.loads(LONGFORM_META_CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Surah metadata cache unreadable, starting fresh: %s", e)
    return {}


def _save_cache(cache: dict) -> None:
    try:
        LONGFORM_META_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        LONGFORM_META_CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("Could not write surah metadata cache: %s", e)


def get_revelation_type(surah_num: int, retries: int = 2) -> str:
    """Meccan / Medinan, fetched once and cached to disk. Returns "Unknown"
    (never raises) if the API is unreachable — this field is presentational
    only and must not block a build."""
    cache = _load_cache()
    key = str(surah_num)
    if key in cache and cache[key].get("revelation_type"):
        return cache[key]["revelation_type"]

    for attempt in range(1, retries + 1):
        try:
            r = requests.get(REVELATION_API_URL.format(n=surah_num), timeout=15)
            r.raise_for_status()
            rev_type = r.json()["data"]["revelationType"]
            rev_type = rev_type.capitalize() if rev_type else "Unknown"
            cache[key] = {**cache.get(key, {}), "revelation_type": rev_type}
            _save_cache(cache)
            return rev_type
        except (requests.RequestException, KeyError, ValueError) as e:
            log.warning("Revelation-type lookup failed for Surah %d (attempt %d/%d): %s",
                        surah_num, attempt, retries, e)
            if attempt < retries:
                time.sleep(2)

    log.warning("Could not determine revelation type for Surah %d — using 'Unknown'.", surah_num)
    return "Unknown"


def get_surah_info(surah_num: int) -> dict:
    """
    Returns full dynamic metadata for one Surah:
      {number, name_en, name_ar, ayah_count, revelation_type, slug}
    Raises SurahNotFoundError for anything outside 1-114.
    """
    if surah_num not in SURAH_MAP:
        raise SurahNotFoundError(f"Surah {surah_num} does not exist (expected 1-114).")

    num, name_en, name_ar, ayah_count = SURAH_MAP[surah_num]
    slug = f"{num:03d}-{name_en.lower().replace(chr(39), '').replace(' ', '-')}"
    return {
        "number": num,
        "name_en": name_en,
        "name_ar": name_ar,
        "ayah_count": ayah_count,
        "revelation_type": get_revelation_type(num),
        "slug": slug,
    }


def all_surah_numbers() -> list:
    return [s[0] for s in SURAHS]
