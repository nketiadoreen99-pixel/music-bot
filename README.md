# Telegram Music Bot — Multi-Platform Search

Searches **SoundCloud**, **Audiomack**, **YouTube Music**, and **Boomplay** in
parallel, ranks results by relevance, and sends the audio file directly to
the user's Telegram chat.

## Features

- **Multi-platform search** — all four sources searched in parallel
- **Smart ranking** — exact title/artist matches, word-level matching,
  recency bonus, remix penalty, popularity bonus
- **Source-aware results** — every result shows which platform it came from
- **Download** — audio sent as a Telegram file (MP3 when FFmpeg is available)
- **Listen** — URL button opens the track on its source platform
- **More Results** — pagination through all combined results
- **Another Source** — filter results to a specific platform
- **Graceful degradation** — one unavailable platform never crashes the bot
- **Search cache** — 5-minute cache avoids duplicate searches
- **Cookie support** — `YOUTUBE_COOKIES` env var for YouTube Music downloads

## Platform status

| Platform | Search | Download | Notes |
|---|---|---|---|
| SoundCloud | ✅ yt-dlp scsearch | ✅ yt-dlp | Best results, always downloadable |
| YouTube Music | ✅ ytmusicapi | ✅ with cookies | `can_download=False` without cookies |
| Audiomack | ⚠️ API/scrape | ✅ yt-dlp | API moved; scraping may fail |
| Boomplay | ⚠️ API/scrape | ❌ listen only | 403 blocks scrapers; returns listening links |

## Requirements

- Python 3.10+
- FFmpeg (recommended for MP3 conversion; bot works without it)

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.

2. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

3. Set the token and start:

   ```powershell
   $env:TELEGRAM_BOT_TOKEN = "your-token-here"
   python music_bot.py
   ```

4. Open the bot in Telegram and send `/start`.

## Usage

| Action | How |
|---|---|
| Search | Type any song name or `Song - Artist` |
| Step-by-step | `/search` then type when prompted |
| Direct URL | Paste a SoundCloud/Audiomack/YouTube URL |
| View details | Tap any result button |
| Download | Tap **Download** (only when the source permits) |
| Listen | Tap **Listen** (opens the source page) |
| More | Tap **More Results** for next page |
| Another source | Tap **Another Source** to filter by platform |
| Cancel | `/cancel` |

## Deploy on Railway

1. Push this repo to GitHub
2. Connect it to Railway
3. Set environment variables:
   - `TELEGRAM_BOT_TOKEN` (or `BOT_TOKEN`) — your BotFather token
   - `YOUTUBE_COOKIES` (optional) — Netscape cookies.txt contents for YouTube Music downloads
4. Start command: `python music_bot.py` (already in `railway.json`)

No API keys are hardcoded. Cookies are read from env vars only.
