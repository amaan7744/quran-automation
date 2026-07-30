# Quran Video Automation

Automated pipeline that recites Quran verses over an edited, moving
nature background and publishes to YouTube Shorts, Facebook Reels, and
Instagram Reels.

> **Visual source note:** this project fetches stock footage from the
> **Pexels** video API (`pexels_fetcher.py`). Pinterest has no public API
> for searching or downloading third-party video content, so there is no
> Pinterest pipeline in this codebase — Pexels is the real source and has
> been hardened with quality scoring, caching, and motion editing below.

## Pipeline

1. **`audio_downloader.py`** — determines the next ayah batch in strict
   Quran order (progress tracked in a GitHub Actions variable + local
   file backup), downloads reciter audio from everyayah.com with retries.
2. **`audio_processor.py`** — masters the concatenated audio: compression,
   EBU R128 loudness normalization, limiter, fade in/out.
3. **`subtitle_builder.py`** — builds an ASS subtitle track with
   word-level karaoke highlighting on the Arabic line, pop-in + fade
   animation, adaptive font sizing, and a strict two-line cap for both
   Arabic and the translation.
4. **`pexels_fetcher.py`** — searches curated nature queries, downloads
   candidates, and quality-gates every clip (resolution, aspect ratio,
   fps, bitrate, blur, freeze detection, shake detection) via
   **`quality_filter.py`**, caching accepted downloads and permanently
   marking used clip IDs so nothing repeats across runs.
5. **`video_effects.py`** — applies a Ken Burns-style zoom/pan/drift
   motion pass to every clip, then joins clips with randomized crossfade
   transitions (never a hard cut).
6. **`build_video.py`** — orchestrates all of the above, detects an
   available hardware H.264 encoder (falls back to libx264), and renders
   the final 1080x1920 video sized to stay under Meta's 100MB Reels limit.
7. **`upload.py`** — publishes to YouTube/Facebook/Instagram with
   retries, generates captions + hashtags, and keeps a persistent
   `upload_history.json` to prevent duplicate publishes.

## Configuration

All tunables (resolution, fps, quality thresholds, fonts, hashtags,
retry counts) live in **`config.py`**.

## Required secrets (GitHub Actions)

| Secret | Purpose |
|---|---|
| `PEXELS_API_KEY` | Stock footage source |
| `GH_PAT` | Progress variable read/write + pushing `progress.json` |
| `YOUTUBE_CLIENT_ID` / `_SECRET` / `_REFRESH_TOKEN` | YouTube upload |
| `META_ACCESS_TOKEN` | Facebook + Instagram publishing (Page token) |
| `FACEBOOK_PAGE_ID` | Facebook Reels target |
| `INSTAGRAM_ACCOUNT_ID` | Instagram Reels target |

## Known limitations / next steps

- **Karaoke word timing is estimated**, not forced-aligned — it
  distributes each ayah's known duration across words by character
  length. For frame-accurate highlighting, run a forced aligner (e.g.
  `aeneas` or `gentle`) offline and feed its word timestamps into
  `subtitle_builder.build_karaoke_text`.
- **Watermark / on-screen-text rejection** isn't implemented — reliably
  detecting overlay text/watermarks needs OCR (e.g. Tesseract) or a
  small CV model, which was left out to keep the dependency footprint
  at just `requests` + ffmpeg. The blur/freeze/shake/resolution/bitrate
  gates in `quality_filter.py` are real and enforced today.
- **Quality thresholds need calibration** against real Pexels footage —
  they were validated against synthetic test clips, not tuned on a
  large real sample.
- **Meta's Page Access Token doesn't have a refresh-token flow** like
  Google's OAuth — it's long-lived but not infinite. It still needs
  periodic manual renewal; automatic refresh isn't something the Graph
  API supports.
