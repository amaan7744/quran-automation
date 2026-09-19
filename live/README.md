# 24/7 Quran YouTube Live

This module runs a continuous Quran recitation livestream using one steady
background and one long-running FFmpeg encoder.

## Architecture

EveryAyah (Saud Al-Shuraim 128kbps)
→ one ayah at a time
→ decoded to raw PCM
→ piped into one long-running FFmpeg process
→ fixed `live_background.png`
→ H.264/AAC
→ YouTube RTMPS

The livestream has its own `stream_state.json`. It does **not** touch
`progress.json` or the `QURAN_PROGRESS` GitHub variable used by Shorts.

## 1. Create the YouTube live stream

In YouTube Studio, create a live stream using an encoder. Copy the stream key.
Do not commit it to GitHub.

Set:

```bash
export YOUTUBE_STREAM_KEY='YOUR_STREAM_KEY'
```

You may also use a `.env`/systemd EnvironmentFile on your VPS.

## 2. Install dependencies

Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y ffmpeg python3 python3-venv
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

The live module uses the existing `requests` dependency. No new Python package
is required.

## 3. Test locally

From the repository root:

```bash
export YOUTUBE_STREAM_KEY='YOUR_STREAM_KEY'
python live/live_stream.py
```

The process should report the current Surah/Ayah and start FFmpeg.

Stop with `Ctrl+C`.

## 4. State and recovery

`live/stream_state.json` stores the **next ayah to play**.

The state is written only after an ayah has been successfully fed to a running
encoder. If the process dies during an ayah, the same ayah is retried after
restart rather than silently skipped.

The small MP3 cache is under `.cache/live_audio/` and is deleted after playback
by default.

## 5. Run permanently with systemd

Copy the service file to:

```bash
sudo cp live/quran-live.service /etc/systemd/system/quran-live.service
```

Edit the service paths/user if your repository is not at `/opt/quran-automation`.

Create the secret environment file:

```bash
sudo mkdir -p /etc/quran-live
sudo nano /etc/quran-live/quran-live.env
```

Contents:

```text
YOUTUBE_STREAM_KEY=YOUR_STREAM_KEY
```

Secure it:

```bash
sudo chmod 600 /etc/quran-live/quran-live.env
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now quran-live
sudo systemctl status quran-live
journalctl -u quran-live -f
```

## Recommended server

For this implementation, use a VPS that stays online 24/7. A personal laptop
and GitHub Actions are not appropriate as the permanent encoder.

The stream is intentionally light on disk because it never builds a multi-GB
full-Quran video. Only the current ayah's MP3 is cached.

## Optional environment variables

```text
YOUTUBE_RTMP_URL=rtmps://a.rtmps.youtube.com/live2
LIVE_RECITER_FOLDER=Saood_ash-Shuraym_128kbps
LIVE_RECITER_NAME=Saud Al-Shuraim
LIVE_VIDEO_BITRATE=4500k
LIVE_VIDEO_MAXRATE=4500k
LIVE_VIDEO_BUFSIZE=9000k
LIVE_AUDIO_BITRATE=160k
LIVE_FPS=30
LIVE_WIDTH=1920
LIVE_HEIGHT=1080
LIVE_DOWNLOAD_RETRIES=5
LIVE_DOWNLOAD_TIMEOUT=45
LIVE_RETRY_BACKOFF_SECONDS=3
LIVE_RESTART_BACKOFF_SECONDS=5
LIVE_KEEP_PLAYED_AUDIO=false
```

## Important

The fixed background is included as `live/live_background.png`. Replace that
file with your own 1920x1080 background if desired; the streaming code does not
need to change.


## GitHub Actions free/chunk mode

If you do not have a VPS, `.github/workflows/quran-live-chunk.yml` can run the
stream for bounded chunks on GitHub-hosted runners. Set the repository secret
`YOUTUBE_STREAM_KEY`. The workflow persists `live/stream_state.json` after each
chunk, so the next run resumes from the next ayah.

This mode is **not a guaranteed uninterrupted 24/7 broadcast**. GitHub-hosted
runners are temporary and GitHub imposes job/runtime limits; the scheduled
workflow intentionally leaves a small gap between chunks. It is useful for
zero-cost testing and for validating the livestream pipeline. A true
uninterrupted 24/7 stream requires an always-on encoder host.

For manual testing, run the workflow from GitHub Actions and choose a duration
up to 350 minutes. The default is 330 minutes.
