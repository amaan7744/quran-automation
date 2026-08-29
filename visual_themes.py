#!/usr/bin/env python3
"""
visual_themes.py
Static "design data" for the cinematic visual engine: visual categories
(search-query pools), category fallback chains, mood -> category
weighting, color grade presets, motion styles, and named video
templates.

This module owns NO logic beyond simple lookups/weighted picks — the
actual decision-making (which mood applies to a given batch of ayahs,
which template gets used, how experimentation is balanced over time)
lives in visual_engine.py and performance_metadata.py. Keeping the raw
data here means those modules (and pexels_fetcher.py) can all reference
one shared, single source of truth for "what visual categories exist
and what do they mean."

Nothing here touches subtitles, Quran text, or audio in any way.
"""

import random

# ══════════════════════════════════════════════════════════════════════════
# VISUAL CATEGORIES
# ══════════════════════════════════════════════════════════════════════════
# Every category is a cinematic, human/vehicle-free visual world. Queries
# are phrased toward slow, drone/cinematic footage — never static "stock
# photo slideshow" framing — and deliberately avoid anything that would
# invite sci-fi elements, literal illustration of a verse, or people.
#
# The five original nature-only categories (forest/mountain/ocean/winter/
# sky_and_night) are kept exactly as they were — they remain valid, good
# options — but they no longer make up the entire system. Eight new
# categories (A-H from the creative brief) sit alongside them.

VISUAL_CATEGORIES = {
    # ─── Legacy (unchanged queries, still fully supported) ────────────────
    "forest": [
        "misty pine forest", "foggy forest morning", "flowing river forest",
        "peaceful forest path", "rain on leaves macro",
        "morning sunlight through trees", "bamboo forest wind",
        "cinematic waterfall forest",
    ],
    "mountain": [
        "cinematic mountain drone", "alpine river drone",
        "clouds above mountains", "sunrise over mountains",
        "aerial valley sunrise", "snow covered mountains",
        "golden hour landscape mountain", "misty mountain peaks",
    ],
    "ocean": [
        "cinematic ocean waves", "aerial coastline drone",
        "sunset over ocean cliffs", "golden hour beach waves",
        "tropical beach aerial drone", "ocean waves cliffs sunset",
    ],
    "winter": [
        "snow falling forest", "frozen lake mist",
        "snow covered mountains drone", "winter fog forest",
        "aurora sky", "moonlit clouds snow",
    ],
    "sky_and_night": [
        "aurora sky timelapse", "stars timelapse night sky",
        "moonlit clouds drifting", "clouds timelapse sky",
        "sunrise golden hour clouds", "night sky stars over mountains",
    ],

    # ─── A. Cosmic / Space ─────────────────────────────────────────────────
    "cosmic": [
        "deep space stars nebula", "galaxy timelapse stars",
        "earth from space", "moon lunar surface cinematic",
        "milky way timelapse", "cosmic dust nebula",
        "stars slowly moving night", "celestial clouds space",
    ],

    # ─── B. Night Sky ──────────────────────────────────────────────────────
    "night_sky": [
        "clear star field night", "moon behind clouds night",
        "stars over mountains night", "moving clouds moonlight",
        "milky way over landscape", "night sky timelapse stars",
    ],

    # ─── C. Atmospheric Light ──────────────────────────────────────────────
    "atmospheric_light": [
        "sunlight through clouds volumetric", "golden light rays clouds",
        "light breaking through darkness", "glowing horizon sunrise",
        "mist illuminated sunlight", "soft light beams clouds",
        "dawn light emerging clouds",
    ],

    # ─── D. Rain / Water ───────────────────────────────────────────────────
    "rain_water": [
        "rain on window glass", "water droplets macro slow motion",
        "slow ocean water movement", "river flowing cinematic",
        "waterfall slow motion cinematic", "water reflection ripples",
        "rain at night cinematic", "calm water moonlight reflection",
    ],

    # ─── E. Desert / Vast Landscapes ───────────────────────────────────────
    "desert": [
        "sand dunes cinematic drone", "desert sunset aerial",
        "desert under stars night", "moonlit desert landscape",
        "wind blowing sand dunes", "vast empty desert landscape",
    ],

    # ─── F. Islamic Architecture ────────────────────────────────────────────
    "islamic_architecture": [
        "mosque exterior architecture cinematic", "islamic geometric pattern",
        "mosque dome architecture", "mosque courtyard empty",
        "minaret architecture sky", "islamic arch architecture detail",
        "geometric shadow pattern architecture",
    ],

    # ─── G. Abstract Spiritual Cinematics ───────────────────────────────────
    "abstract_spiritual": [
        "floating dust particles light", "smoke atmosphere slow motion",
        "glowing particles dark background", "ink diffusion water slow motion",
        "soft bokeh dark background", "slow moving texture abstract dark",
        "light trails slow motion subtle",
    ],

    # ─── H. Clouds / Heaven-like Atmosphere ─────────────────────────────────
    "clouds_heaven": [
        "enormous clouds timelapse", "clouds above mountains cinematic",
        "sunlight through clouds cinematic", "cloud timelapse sky",
        "moonlit clouds drifting slow", "blue hour clouds sky",
        "dramatic peaceful sky clouds",
    ],
}

# Every category the fallback/general pool is allowed to draw from when a
# primary + related category both come up short. Deliberately restricted
# to calm, unmistakably "safe" cinematic footage rather than the full
# category list, so the very last resort still can't jump somewhere
# visually jarring.
GENERAL_CINEMATIC_POOL = [
    "clouds_heaven", "mountain", "ocean", "night_sky",
]

# Nearest-neighbor category for the fallback chain (see item 11: primary
# -> same-category alternate queries [handled by pexels_fetcher's own
# multi-round search] -> related category -> general cinematic category).
# Chosen so a fallback never visually "jumps" (e.g. cosmic never falls
# back to rain_water) — see item 11 of the brief.
RELATED_CATEGORY = {
    "forest": "mountain",
    "mountain": "clouds_heaven",
    "ocean": "rain_water",
    "winter": "night_sky",
    "sky_and_night": "night_sky",
    "cosmic": "night_sky",
    "night_sky": "cosmic",
    "atmospheric_light": "clouds_heaven",
    "rain_water": "ocean",
    "desert": "night_sky",
    "islamic_architecture": "atmospheric_light",
    "abstract_spiritual": "night_sky",
    "clouds_heaven": "atmospheric_light",
}


def fallback_chain(primary: str) -> list:
    """
    Returns the ordered list of categories to try for one reel:
    [primary, related, general...]. Duplicates are removed while
    preserving order, so a related category that's already in the
    general pool isn't searched twice.
    """
    chain = [primary]
    related = RELATED_CATEGORY.get(primary)
    if related and related not in chain:
        chain.append(related)
    for cat in GENERAL_CINEMATIC_POOL:
        if cat not in chain:
            chain.append(cat)
    return chain


# ══════════════════════════════════════════════════════════════════════════
# MOOD -> CATEGORY WEIGHTING
# ══════════════════════════════════════════════════════════════════════════
# Weighted PREFERENCES, not hard rules — pick_category_for_mood() below
# always keeps some randomness so the same mood doesn't always render
# identically. These weights encode simple, honestly-approximate
# associations (see item 3 of the brief); they do not claim to
# understand tafsir/theological nuance.

MOOD_CATEGORY_WEIGHTS = {
    "peace":       {"clouds_heaven": 3, "night_sky": 2, "atmospheric_light": 2, "ocean": 1},
    "awe":         {"cosmic": 3, "mountain": 2, "desert": 1, "clouds_heaven": 1},
    "hope":        {"atmospheric_light": 3, "clouds_heaven": 2, "ocean": 1},
    "mercy":       {"atmospheric_light": 3, "rain_water": 2, "clouds_heaven": 2},
    "reflection":  {"night_sky": 3, "rain_water": 2, "abstract_spiritual": 2},
    "patience":    {"mountain": 3, "desert": 2, "rain_water": 1},
    "warning":     {"night_sky": 2, "desert": 2, "abstract_spiritual": 2},
    "repentance":  {"rain_water": 3, "night_sky": 2, "atmospheric_light": 1},
    "gratitude":   {"clouds_heaven": 2, "atmospheric_light": 2, "mountain": 1},
    "akhirah":     {"cosmic": 3, "desert": 2, "night_sky": 2},
    "creation":    {"cosmic": 3, "mountain": 2, "sky_and_night": 1},
    "night":       {"night_sky": 3, "cosmic": 2},
    "protection":  {"mountain": 3, "islamic_architecture": 2},
    "trust":       {"clouds_heaven": 2, "mountain": 2, "ocean": 1},
    "hardship":    {"mountain": 3, "desert": 2, "rain_water": 1},
    "forgiveness": {"rain_water": 3, "atmospheric_light": 2, "clouds_heaven": 1},
}

DEFAULT_MOOD = "reflection"

# Simple, deterministic English-keyword -> mood signal used by
# visual_engine.detect_mood(). Intentionally coarse: this is metadata for
# picking a visual mood, not a claim of theological understanding. A
# batch can score for multiple moods; the highest-scoring one wins (see
# visual_engine.py), with ties broken randomly to keep some variety.
MOOD_KEYWORDS = {
    "peace":       ["peace", "tranquil", "calm", "rest", "serenity"],
    "awe":         ["created", "creation", "vast", "power", "glory", "majesty", "heavens and earth"],
    "hope":        ["hope", "glad tiding", "good news", "reward", "paradise", "garden"],
    "mercy":       ["mercy", "merciful", "compassion", "forgiv", "gracious"],
    "reflection":  ["reflect", "ponder", "think", "sign", "signs", "remember", "remind"],
    "patience":    ["patien", "persever", "endure", "steadfast"],
    "warning":     ["warn", "punishm", "fire", "torment", "wrongdo", "disbeliev"],
    "repentance":  ["repent", "turn back", "forgiveness", "sin"],
    "gratitude":   ["grateful", "thank", "bless", "favor", "favour"],
    "akhirah":     ["hereafter", "judgment", "judgement", "resurrection", "afterlife", "day of"],
    "creation":    ["created", "creation", "heavens and earth", "made", "fashioned"],
    "night":       ["night", "darkness", "moon", "star"],
    "protection":  ["protect", "refuge", "shield", "guard"],
    "trust":       ["trust", "rely", "reliance", "guardian"],
    "hardship":    ["hardship", "difficulty", "trial", "test", "affliction"],
    "forgiveness": ["forgiv", "pardon", "absolve"],
}


def pick_category_for_mood(mood: str, rng: random.Random = None) -> str:
    """Weighted-random category pick for a given mood. Falls back to the
    default mood's weights (and finally to a flat random category) if the
    mood is unrecognized, so this never raises on unexpected input."""
    rng = rng or random
    weights = MOOD_CATEGORY_WEIGHTS.get(mood) or MOOD_CATEGORY_WEIGHTS.get(DEFAULT_MOOD)
    if not weights:
        return rng.choice(list(VISUAL_CATEGORIES.keys()))
    categories, weight_values = zip(*weights.items())
    return rng.choices(categories, weights=weight_values, k=1)[0]


# ══════════════════════════════════════════════════════════════════════════
# COLOR GRADES
# ══════════════════════════════════════════════════════════════════════════
# Each grade is a set of parameters consumed by pexels_fetcher.trim_and_
# normalize() in place of the old single fixed GRADE_* constants. Kept
# gentle across the board — see item 9: "Avoid overprocessed TikTok-style
# colors."

COLOR_GRADES = {
    "deep_night": dict(contrast=1.05, saturation=0.85, brightness=-0.02,
                        shadow_warmth=-0.04, highlight_warmth=0.00),
    "midnight_blue": dict(contrast=1.04, saturation=0.88, brightness=-0.01,
                           shadow_warmth=-0.05, highlight_warmth=0.01),
    "warm_gold": dict(contrast=1.05, saturation=1.00, brightness=0.02,
                       shadow_warmth=-0.01, highlight_warmth=0.05),
    "moonlight": dict(contrast=1.03, saturation=0.80, brightness=0.00,
                       shadow_warmth=-0.06, highlight_warmth=-0.01),
    "neutral_cinematic": dict(contrast=1.04, saturation=0.94, brightness=0.01,
                               shadow_warmth=-0.02, highlight_warmth=0.02),
    "soft_teal": dict(contrast=1.05, saturation=0.90, brightness=0.00,
                       shadow_warmth=-0.05, highlight_warmth=0.015),
    "dawn": dict(contrast=1.03, saturation=0.95, brightness=0.015,
                 shadow_warmth=-0.01, highlight_warmth=0.04),
    "desert_warmth": dict(contrast=1.05, saturation=0.97, brightness=0.015,
                           shadow_warmth=0.01, highlight_warmth=0.05),
}

# Which grades read naturally for each category. Used to pick a sensible
# default grade per template; templates may still override explicitly.
CATEGORY_GRADE_PREFERENCE = {
    "forest": "neutral_cinematic", "mountain": "neutral_cinematic",
    "ocean": "soft_teal", "winter": "moonlight", "sky_and_night": "midnight_blue",
    "cosmic": "deep_night", "night_sky": "midnight_blue",
    "atmospheric_light": "warm_gold", "rain_water": "soft_teal",
    "desert": "desert_warmth", "islamic_architecture": "warm_gold",
    "abstract_spiritual": "deep_night", "clouds_heaven": "dawn",
}


# ══════════════════════════════════════════════════════════════════════════
# MOTION STYLES
# ══════════════════════════════════════════════════════════════════════════
# Consumed by video_effects.apply_motion(). "static" is a genuine
# no-motion option — see item 8: not every clip should move.

MOTION_STYLES = [
    "push_in", "pull_out", "drift_horizontal", "drift_vertical",
    "drift_diagonal", "static", "subtle_rotate",
]

# ══════════════════════════════════════════════════════════════════════════
# TRANSITION STYLES
# ══════════════════════════════════════════════════════════════════════════
TRANSITION_STYLES = ["fade", "fadeblack", "dissolve", "smoothleft", "smoothright"]


# ══════════════════════════════════════════════════════════════════════════
# TEMPLATES
# ══════════════════════════════════════════════════════════════════════════
# A template bundles a preferred category, motion pool, transition pool,
# color grade, clip-duration range, and atmosphere intensity into one
# named, reusable "look." visual_engine.py selects a template (biased by
# mood + past performance); pexels_fetcher/video_effects consume its
# fields. Every template still ultimately reduces to the same
# category/grade/motion primitives above, so nothing here needs its own
# separate implementation.

TEMPLATES = {
    "TEMPLATE_01_COSMIC": dict(
        category="cosmic", color_grade="deep_night",
        motion_styles=["drift_diagonal", "static", "push_in"],
        transition_styles=["fade", "fadeblack"],
        clip_duration_range=(4.0, 6.0), atmosphere_intensity="high",
    ),
    "TEMPLATE_02_MOONLIGHT": dict(
        category="night_sky", color_grade="moonlight",
        motion_styles=["drift_horizontal", "static", "pull_out"],
        transition_styles=["fade", "dissolve"],
        clip_duration_range=(3.5, 5.5), atmosphere_intensity="medium",
    ),
    "TEMPLATE_03_LIGHT_BREAKING": dict(
        category="atmospheric_light", color_grade="warm_gold",
        motion_styles=["push_in", "drift_vertical"],
        transition_styles=["dissolve", "fade"],
        clip_duration_range=(3.5, 5.5), atmosphere_intensity="medium",
    ),
    "TEMPLATE_04_RAIN": dict(
        category="rain_water", color_grade="soft_teal",
        motion_styles=["static", "drift_horizontal"],
        transition_styles=["dissolve", "smoothleft"],
        clip_duration_range=(3.0, 5.0), atmosphere_intensity="low",
    ),
    "TEMPLATE_05_DESERT_NIGHT": dict(
        category="desert", color_grade="desert_warmth",
        motion_styles=["drift_diagonal", "pull_out", "static"],
        transition_styles=["fade", "smoothright"],
        clip_duration_range=(4.0, 6.0), atmosphere_intensity="medium",
    ),
    "TEMPLATE_06_ISLAMIC_ARCHITECTURE": dict(
        category="islamic_architecture", color_grade="warm_gold",
        motion_styles=["static", "subtle_rotate", "push_in"],
        transition_styles=["dissolve", "fade"],
        clip_duration_range=(3.5, 5.5), atmosphere_intensity="low",
    ),
    "TEMPLATE_07_DEEP_WATER": dict(
        category="rain_water", color_grade="midnight_blue",
        motion_styles=["drift_vertical", "static"],
        transition_styles=["dissolve", "fade"],
        clip_duration_range=(3.5, 5.5), atmosphere_intensity="low",
    ),
    "TEMPLATE_08_CLOUDS": dict(
        category="clouds_heaven", color_grade="dawn",
        motion_styles=["drift_horizontal", "pull_out", "push_in"],
        transition_styles=["fade", "dissolve"],
        clip_duration_range=(4.0, 6.0), atmosphere_intensity="medium",
    ),
    "TEMPLATE_09_ABSTRACT_ATMOSPHERE": dict(
        category="abstract_spiritual", color_grade="deep_night",
        motion_styles=["static", "drift_diagonal", "subtle_rotate"],
        transition_styles=["dissolve", "fadeblack"],
        clip_duration_range=(3.0, 5.0), atmosphere_intensity="high",
    ),
    "TEMPLATE_10_CINEMATIC_NATURE": dict(
        category="mountain", color_grade="neutral_cinematic",
        motion_styles=["push_in", "pull_out", "drift_horizontal", "static"],
        transition_styles=["fade", "smoothleft", "smoothright"],
        clip_duration_range=(3.0, 5.0), atmosphere_intensity="medium",
    ),
}

TEMPLATE_NAMES = list(TEMPLATES.keys())
