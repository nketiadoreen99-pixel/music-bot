"""
Telegram YouTube Music Bot
- Searches YouTube Music (unofficial ytmusicapi)
- Shows song details with artwork before downloading
- Downloads audio via yt-dlp and sends the file to the user
"""

import asyncio
import functools
import html
import logging
import os
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import yt_dlp
from ytmusicapi import YTMusic
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_RESULTS = 8
MAX_FILE_MB = 48          # stay under Telegram's 50 MB bot upload limit
ASK_SONG    = 0           # ConversationHandler state

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Shared YTMusic client — no auth needed for public search
_ytm = YTMusic()


# ── Formatting helpers ────────────────────────────────────────────────────────

def fmt_artists(artists: list[dict]) -> str:
    return ", ".join(a.get("name", "") for a in artists if a.get("name"))


def fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def best_thumbnail(thumbnails: list[dict]) -> str:
    """Return the highest-resolution thumbnail URL from a ytmusicapi list."""
    if not thumbnails:
        return ""
    # ytmusicapi returns thumbnails sorted smallest → largest; take the last
    return thumbnails[-1].get("url", "")


# ── YTMusic search ────────────────────────────────────────────────────────────

def ytm_search(query: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    """Return up to *limit* song dicts from YouTube Music."""
    try:
        results = _ytm.search(query, filter="songs", limit=limit)
        return [r for r in results if r.get("videoId")]
    except Exception:
        logger.exception("YTMusic search failed for %r", query)
        return []


# ── yt-dlp audio download ─────────────────────────────────────────────────────

# User-agent that resembles a real browser to reduce YouTube bot-detection hits
_YT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# If YTDL_PROXY is set in the environment (e.g. on Railway), pass it to yt-dlp
_PROXY = os.environ.get("YTDL_PROXY") or None


def _ydl_base_opts(outtmpl: str) -> dict[str, Any]:
    """Common yt-dlp options shared by all download attempts."""
    opts: dict[str, Any] = {
        "format":        "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "outtmpl":       outtmpl,
        "quiet":         True,
        "no_warnings":   True,
        "noplaylist":    True,
        "max_filesize":  MAX_FILE_MB * 1024 * 1024,
        # Use the music.youtube.com domain — slightly less aggressive bot-detection
        "source_address": "0.0.0.0",
        "http_headers":  _YT_HEADERS,
        # Extractor args to bypass some bot checks
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "android"],
                "skip": ["dash", "hls"],
            }
        },
        # Retry on transient network errors
        "retries":        5,
        "fragment_retries": 5,
    }
    if _PROXY:
        opts["proxy"] = _PROXY
    return opts


def _ffmpeg_available() -> bool:
    import shutil as _shutil
    return bool(_shutil.which("ffmpeg"))


def download_audio(video_id: str, tmpdir: str) -> tuple[str, dict]:
    """
    Download the best available audio stream for *video_id* into *tmpdir*.

    Strategy:
      1. Try music.youtube.com URL (less bot-detection than youtube.com)
      2. Fall back to youtube.com URL
      3. If FFmpeg is present, remux to MP3; otherwise keep the native format
         (m4a/webm — Telegram plays both fine as audio files)

    Returns (file_path, yt_dlp_info_dict).
    Raises RuntimeError on failure or when the file exceeds MAX_FILE_MB.
    """
    # Prefer the music subdomain — fewer bot-detection challenges on servers
    urls = [
        f"https://music.youtube.com/watch?v={video_id}",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    has_ffmpeg = _ffmpeg_available()
    logger.info("FFmpeg available: %s", has_ffmpeg)

    base = _ydl_base_opts(outtmpl)

    if has_ffmpeg:
        base["postprocessors"] = [{
            "key":              "FFmpegExtractAudio",
            "preferredcodec":   "mp3",
            "preferredquality": "192",
        }]

    info: dict = {}
    last_error: str = ""

    for url in urls:
        try:
            with yt_dlp.YoutubeDL(base) as ydl:
                info = ydl.extract_info(url, download=True) or {}
            # Success — break out of the retry loop
            break
        except yt_dlp.utils.DownloadError as exc:
            last_error = str(exc)
            err_lower  = last_error.lower()

            # If the error is specifically about FFmpeg, strip the postprocessor
            # and retry the same URL without it
            if has_ffmpeg and ("ffmpeg" in err_lower or "ffprobe" in err_lower):
                logger.warning("FFmpeg post-processing failed, retrying without it: %s", exc)
                opts_no_ff = {k: v for k, v in base.items() if k != "postprocessors"}
                try:
                    with yt_dlp.YoutubeDL(opts_no_ff) as ydl:
                        info = ydl.extract_info(url, download=True) or {}
                    break
                except yt_dlp.utils.DownloadError as exc2:
                    last_error = str(exc2)
                    continue

            # Bot-detection / sign-in errors — try the next URL
            if any(kw in err_lower for kw in ("sign in", "bot", "confirm", "429", "403")):
                logger.warning("Bot-detection hit for %s, trying next URL: %s", url, exc)
                continue

            # Any other download error — give up immediately
            raise RuntimeError(last_error) from exc
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc
    else:
        # All URLs exhausted
        raise RuntimeError(
            "YouTube is blocking downloads from this server. "
            f"Last error: {last_error}"
        )

    # Locate the downloaded file
    path = ""
    if has_ffmpeg:
        mp3 = os.path.join(tmpdir, f"{video_id}.mp3")
        path = mp3 if os.path.exists(mp3) else ""

    if not path:
        try:
            path = yt_dlp.YoutubeDL(base).prepare_filename(info)
        except Exception:
            path = ""

    if not path or not os.path.exists(path):
        candidates = sorted(
            [f for f in os.listdir(tmpdir)
             if not f.endswith(".part") and not f.endswith(".ytdl")],
            key=lambda f: os.path.getsize(os.path.join(tmpdir, f)),
            reverse=True,
        )
        if not candidates:
            raise RuntimeError("Download produced no output file.")
        path = os.path.join(tmpdir, candidates[0])

    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb > MAX_FILE_MB:
        raise RuntimeError(
            f"Track is {size_mb:.0f} MB — too large for Telegram (limit {MAX_FILE_MB} MB)."
        )

    logger.info("Downloaded %s → %s (%.1f MB)", video_id, path, size_mb)
    return path, info


# ── UI builders ───────────────────────────────────────────────────────────────

def build_results_message(tracks: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    """
    Build the search-results list message and its inline keyboard.
    Each button opens the song detail view (shows info + download option).
    """
    lines: list[str] = ["<b>🎵 Search results</b>\n<i>Tap a song to see details and download it.</i>\n"]
    buttons: list[list[InlineKeyboardButton]] = []

    for i, track in enumerate(tracks, start=1):
        title   = html.escape(track.get("title") or "Unknown")
        artists = html.escape(fmt_artists(track.get("artists") or []) or "Unknown")
        album_d = track.get("album") or {}
        album   = html.escape(str(album_d.get("name") or ""))
        dur     = fmt_duration(track.get("duration_seconds"))
        views   = track.get("views") or ""

        line = f"{i}. <b>{title}</b> — {artists}"
        if album:
            line += f"\n    <i>{album}</i>"
        extras = "  ·  ".join(filter(None, [dur, (f"{views} views" if views else "")]))
        if extras:
            line += f"\n    {extras}"
        lines.append(line)

        # callback_data must be ≤ 64 bytes; videoId is always 11 chars
        vid = track["videoId"]
        label = f"{i}. {track.get('title','')[:25]} — {fmt_artists(track.get('artists') or [])[:18]}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"info:{vid}")])

    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def build_song_detail(track: dict, video_id: str) -> tuple[str, InlineKeyboardMarkup]:
    """
    Build the per-song detail card text and action keyboard.
    """
    title   = html.escape(track.get("title") or "Unknown")
    artists = html.escape(fmt_artists(track.get("artists") or []) or "Unknown")
    album_d = track.get("album") or {}
    album   = html.escape(str(album_d.get("name") or ""))
    dur     = fmt_duration(track.get("duration_seconds"))
    views   = track.get("views") or ""
    yr      = track.get("year") or ""

    lines = [f"🎵 <b>{title}</b>", f"👤 {artists}"]
    if album:
        lines.append(f"💿 {album}")
    if dur:
        lines.append(f"⏱ {dur}")
    if yr:
        lines.append(f"📅 {yr}")
    if views:
        lines.append(f"👁 {views} views")
    lines.append(f"\n<a href='https://music.youtube.com/watch?v={video_id}'>Open in YouTube Music</a>")

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Download & Send Audio", callback_data=f"dl:{video_id}")],
        [InlineKeyboardButton("🔙 Back to results",       callback_data="back")],
    ])
    return "\n".join(lines), keyboard


# ── Telegram handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text(
        "🎵 <b>YouTube Music Bot</b>\n\n"
        "Search YouTube Music and get the audio file sent straight to you.\n\n"
        "• Type any song name or <code>Song - Artist</code>\n"
        "• Or use /search for step-by-step prompts\n"
        "• /help — show this message again",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if not msg:
        return ConversationHandler.END

    if context.args:
        await _do_search(msg, " ".join(context.args).strip(), context)
        return ConversationHandler.END

    await msg.reply_text("What song are you looking for? (or /cancel)")
    return ASK_SONG


async def receive_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if not msg or not msg.text:
        return ASK_SONG
    await _do_search(msg, msg.text.strip(), context)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if msg:
        await msg.reply_text("Cancelled.")
    return ConversationHandler.END


async def handle_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return
    await _do_search(msg, msg.text.strip(), context)


async def _do_search(msg: Any, query: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run a YTMusic search and display the results keyboard."""
    if not query:
        await msg.reply_text("Please send a song name.")
        return

    status = await msg.reply_text("🔍 Searching YouTube Music…")
    tracks = await _run_blocking(ytm_search, query)

    if not tracks:
        await _safe_edit(status, f"No results for <b>{html.escape(query)}</b>. Try different words.")
        return

    # Cache results so the detail view and back-button can reuse them
    # without another network call.  Key = chat_id.
    context.chat_data["last_tracks"]  = tracks
    context.chat_data["last_query"]   = query

    text, keyboard = build_results_message(tracks)
    await _safe_edit(status, text, keyboard)


# ── Callback query handlers ───────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Single entry-point for all inline keyboard callbacks.

    Patterns handled:
        info:<videoId>   — show song detail card
        dl:<videoId>     — download and send audio
        back             — go back to the last results list
        cancel           — dismiss the message
    """
    cq = update.callback_query
    if not cq:
        return

    # Always answer the callback query immediately so Telegram stops showing
    # the loading spinner on the button — this must happen within 10 seconds.
    try:
        await cq.answer()
    except Exception:
        pass  # Already answered or timed out — not fatal

    data = (cq.data or "").strip()

    # ── cancel ────────────────────────────────────────────────────────────────
    if data == "cancel":
        await _safe_edit(cq.message, "Cancelled. Type a song name to search again.")
        return

    # ── back to results ───────────────────────────────────────────────────────
    if data == "back":
        tracks = context.chat_data.get("last_tracks")
        if not tracks:
            await _safe_edit(cq.message, "Session expired. Please search again.")
            return
        text, keyboard = build_results_message(tracks)
        await _safe_edit(cq.message, text, keyboard)
        return

    # ── song detail view ──────────────────────────────────────────────────────
    if data.startswith("info:"):
        video_id = data[5:]
        if not video_id:
            await _safe_edit(cq.message, "❌ Invalid song ID.")
            return

        tracks = context.chat_data.get("last_tracks") or []
        track  = next((t for t in tracks if t.get("videoId") == video_id), None)

        if not track:
            await _safe_edit(cq.message, "❌ Could not find that song. Please search again.")
            return

        text, keyboard = build_song_detail(track, video_id)
        thumb = best_thumbnail(track.get("thumbnails") or [])

        if thumb:
            # Try to send a new photo message with the detail card as caption.
            # If that fails (e.g. the URL is invalid), fall back to plain text.
            try:
                await cq.message.reply_photo(
                    photo=thumb,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                # Edit the original results message to a neutral prompt so the
                # chat doesn't look cluttered.
                await _safe_edit(
                    cq.message,
                    "⬆️ Song details above. Tap <b>Back to results</b> to search again.",
                )
                return
            except Exception:
                pass  # Fall through to plain-text edit

        # Plain-text fallback (no thumbnail or photo send failed)
        await _safe_edit(cq.message, text, keyboard)
        return

    # ── download & send ───────────────────────────────────────────────────────
    if data.startswith("dl:"):
        video_id = data[3:]
        if not video_id:
            await _safe_edit(cq.message, "❌ Invalid song ID.")
            return

        await _safe_edit(cq.message, "⬇️ Downloading audio — this usually takes 10–30 seconds…")

        chat_id = cq.message.chat_id if cq.message else None
        if not chat_id:
            return

        tmpdir = tempfile.mkdtemp(prefix="ytbot_", dir="/tmp")
        try:
            path, dl_info = await _run_blocking(download_audio, video_id, tmpdir)

            # Build caption from yt-dlp metadata
            title    = dl_info.get("title")    or ""
            artist   = dl_info.get("artist")   or dl_info.get("uploader") or ""
            album    = dl_info.get("album")     or ""
            dur_secs = dl_info.get("duration")

            parts: list[str] = []
            if title:
                parts.append(f"🎵 <b>{html.escape(title)}</b>")
            if artist:
                parts.append(f"👤 {html.escape(artist)}")
            if album:
                parts.append(f"💿 {html.escape(album)}")
            d = fmt_duration(int(dur_secs) if dur_secs else None)
            if d:
                parts.append(f"⏱ {d}")
            parts.append(
                f"\n<a href='https://music.youtube.com/watch?v={video_id}'>"
                f"Open in YouTube Music</a>"
            )
            caption = "\n".join(parts)

            await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)

            with open(path, "rb") as f:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=f,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    title=title or None,
                    performer=artist or None,
                    duration=int(dur_secs) if dur_secs else None,
                )

            await _safe_edit(
                cq.message,
                "✅ Done! Type another song name or use /search to find more music.",
            )

        except RuntimeError as exc:
            logger.warning("Download failed for %s: %s", video_id, exc)
            await _safe_edit(cq.message, f"❌ {exc}")
        except Exception:
            logger.exception("Unexpected error downloading videoId=%s", video_id)
            await _safe_edit(
                cq.message,
                "❌ Something went wrong. Please try again or search for a different song.",
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return

    # Unknown callback — ignore silently
    logger.warning("Unhandled callback data: %r", data)


# ── Safe message edit helper ──────────────────────────────────────────────────

async def _safe_edit(
    message: Any,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """
    Edit *message* text, swallowing harmless Telegram errors.

    - BadRequest "message is not modified"  → silent skip
    - BadRequest other                       → log and skip
    - RetryAfter / TimedOut                  → log and skip
    """
    try:
        await message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.warning("edit_text BadRequest: %s", exc)
    except (RetryAfter, TimedOut) as exc:
        logger.warning("edit_text rate-limit/timeout: %s", exc)
    except Exception as exc:
        logger.warning("edit_text failed: %s", exc)


# ── Thread-pool helper ────────────────────────────────────────────────────────

async def _run_blocking(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking / CPU-bound function in the default thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


# ── Railway health-check server ───────────────────────────────────────────────

def _start_health_server() -> None:
    """
    Spin up a minimal HTTP server on $PORT (default 8080) in a daemon thread
    so Railway's health-check passes.  The bot itself uses long polling and
    does not need this port for Telegram traffic.
    """
    port = int(os.environ.get("PORT", 8080))

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *args):
            pass  # silence access logs

    server = HTTPServer(("0.0.0.0", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info("Health-check server listening on port %d", port)


# ── Error handler ─────────────────────────────────────────────────────────────

async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled update error", exc_info=context.error)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Support both Railway's default BOT_TOKEN and the explicit name
    token = (
        os.environ.get("TELEGRAM_BOT_TOKEN")
        or os.environ.get("BOT_TOKEN")
    )
    if not token:
        raise SystemExit(
            "Set TELEGRAM_BOT_TOKEN (or BOT_TOKEN) to your BotFather token."
        )

    _start_health_server()

    app = Application.builder().token(token).build()

    # /search conversation
    search_conv = ConversationHandler(
        entry_points=[CommandHandler("search", cmd_search)],
        states={
            ASK_SONG: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_query)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(search_conv)

    # One handler covers ALL callback patterns: info:, dl:, back, cancel
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Plain text outside any conversation → direct search
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_text))

    app.add_error_handler(handle_error)

    logger.info("Bot starting — polling for updates…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
