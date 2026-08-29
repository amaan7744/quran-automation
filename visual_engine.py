#!/usr/bin/env python3
"""
visual_engine.py
The "visual mood engine" from item 3 of the brief:

    Quran content -> visual mood -> visual category -> search queries
    -> clip selection -> motion style -> color treatment -> transition
    style

This module decides WHAT to build (mood, template, category, fallback
chain, motion/transition/color choices); pexels_fetcher.py and
video_effects.py do the actual fetching/rendering. Nothing here touches
subtitles or Quran text content — it only *reads* the English
translation text already loaded by build_video.py to make a coarse,
honestly-approximate mood guess.
"""

import random
from pathlib import Path

from config import VIDEO_FPS
from visual_themes import (
    MOOD_KEYWORDS, DEFAULT_MOOD, TEMPLATES, CATEGORY_GRADE_PREFERENCE,
    fallback_chain,
)
from performance_metadata import pick_template
from pexels_fetcher import build_background
from logging_utils import get_logger

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════
# MOOD DETECTION
# ══════════════════════════════════════════════════════════════════════════

def detect_mood(english_texts: list, rng: random.Random = None) -> str:
    """
    Deterministic, keyword-based mood scoring over the batch's English
    translation text (see MOOD_KEYWORDS in visual_themes.py). This is
    metadata for picking a visual mood, NOT a theological interpretation
    of the ayah — ties and no-match cases fall back to a random-ish
    default so the system still varies.
    """
    rng = rng or random
    combined = " ".join(t.lower() for t in english_texts if t)
    if not combined.strip():
        return DEFAULT_MOOD

    scores = {}
    for mood, keywords in MOOD_KEYWORDS.items():
        hits = sum(combined.count(kw) for kw in keywords)
        if hits:
            scores[mood] = hits

    if not scores:
        return DEFAULT_MOOD

    top_score = max(scores.values())
    top_moods = [m for m, s in scores.items() if s == top_score]
    return rng.choice(top_moods)


# ══════════════════════════════════════════════════════════════════════════
# TEMPLATE / PLAN SELECTION
# ══════════════════════════════════════════════════════════════════════════

def choose_plan(english_texts: list, rng: random.Random = None) -> dict:
    """
    Picks the mood, template, and every downstream visual choice for one
    reel. Returns a plan dict consumed by build_background_for_plan()
    and later folded into the video's saved metadata.
    """
    rng = rng or random
    mood = detect_mood(english_texts, rng)
    template_name = pick_template(mood, rng)
    template = TEMPLATES[template_name]

    category = template["category"]
    color_grade = template.get("color_grade") or CATEGORY_GRADE_PREFERENCE.get(category, "neutral_cinematic")
    motion_pool = template["motion_styles"]
    transition_pool = template["transition_styles"]
    transition_style = rng.choice(transition_pool)
    clip_duration_range = template["clip_duration_range"]
    atmosphere_intensity = template["atmosphere_intensity"]

    plan = {
        "mood": mood,
        "visual_template": template_name,
        "visual_category": category,
        "category_fallback_chain": fallback_chain(category),
        "color_grade": color_grade,
        "motion_pool": motion_pool,
        "transition_style": transition_style,
        "clip_duration_range": clip_duration_range,
        "atmosphere_intensity": atmosphere_intensity,
    }
    log.info(
        "Visual plan: mood=%s template=%s category=%s grade=%s transition=%s",
        mood, template_name, category, color_grade, transition_style,
    )
    return plan


# ══════════════════════════════════════════════════════════════════════════
# BACKGROUND BUILD ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

def build_background_for_plan(plan: dict, total_duration: float, tmpdir: Path, out_path: Path) -> dict:
    """
    Thin wrapper around pexels_fetcher.build_background() that passes
    through everything the plan decided (category fallback chain, color
    grade, motion pool, transition style, per-clip duration range).
    Returns {"motion_styles": [...], "source_clips": [...], "category_used": ...}
    — pexels_fetcher records all of this as it goes; build_video.py uses
    it for the video's metadata (item 3/10/13 of the pipeline).
    """
    return build_background(
        total_duration=total_duration,
        tmpdir=tmpdir,
        out_path=out_path,
        category_chain=plan["category_fallback_chain"],
        color_grade=plan["color_grade"],
        motion_pool=plan["motion_pool"],
        transition_style=plan["transition_style"],
        clip_duration_range=plan["clip_duration_range"],
        fps=VIDEO_FPS,
    )
