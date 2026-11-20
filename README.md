# Telegram Music Bot (SoundCloud)

Searches SoundCloud via yt-dlp and sends the audio file directly to the user's Telegram chat.

## Features

- Search by any text — song name, artist, or both
- Paste a SoundCloud track URL to download it directly
- Results shown as an inline keyboard — tap any result to see details and download
- Audio sent as a Telegram audio file (MP3, saved in the user's chat)
- Cleans up `/tmp` after every send
- FFmpeg converts to MP3 at 192 kbps; works without FFmpeg too (sends native format)

## Requirements

- Python 3.10 or newer
- FFmpeg on PATH (recommended — installed automatically on Railway via `nixpacks.toml`)

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
| Direct URL | Paste a `soundcloud.com` track URL |
| Download | Tap a result → tap **Download & Send Audio** |
| Cancel | `/cancel` during a search |

## Deploy on Railway

Push this repo to GitHub, connect it to Railway, and set `TELEGRAM_BOT_TOKEN`
as an environment variable. `nixpacks.toml` installs FFmpeg automatically.
The bot uses long polling — no webhook or public URL needed.
