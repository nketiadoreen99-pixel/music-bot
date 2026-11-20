"""
Telegram Music Bot
- Searches SoundCloud via yt-dlp scsearch
- Downloads audio via yt-dlp (bestaudio/best → FFmpegExtractAudio → mp3)
- Sends the mp3 file to the user, then deletes it from /tmp
"""

import asyncio
import functools
import html
import logging
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse

import yt_dlp
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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

# ── Logging — everything goes to stdout so Railway captures it ────────────────

logging.basicConfig(
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
    force=True,
)
logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_RESULTS = 5    # scsearch returns at most this many
MAX_FILE_MB = 48   # under Telegram's 50 MB bot upload limit
ASK_SONG    = 0    # ConversationHandler state

SC_SEARCH_LIMIT = MAX_RESULTS


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _ffmpeg_available() -> bool:
    import shutil as _sh
    return bool(_sh.which("ffmpeg"))


def _is_sc_url(text: str) -> bool:
    """Return True if *text* looks like a soundcloud.com track URL."""
    try:
        p = urlparse(text.strip())
        return p.scheme in ("http", "https") and "soundcloud.com" in p.netloc
    except Exception:
        return False


# ── SoundCloud search ─────────────────────────────────────────────────────────

def sc_search(query: str, limit: int = SC_SEARCH_LIMIT) -> list[dict[str, Any]]:
    """
    Search SoundCloud for *query* using yt-dlp's scsearch extractor.
    Returns a list of track info dicts with at least: id, title, uploader,
    webpage_url, duration, thumbnail.
    """
    search_url = f"scsearch{limit}:{query}"
    ydl_opts: dict[str, Any] = {
        "quiet":       False,
        "no_warnings": False,
        "extract_flat": True,   # metadata only, no download
        "noplaylist":  False,   # scsearch IS a playlist
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_url, download=False) or {}
        entries = info.get("entries") or []
        return [e for e in entries if e and e.get("id")]
    except Exception:
        logger.exception("SoundCloud search failed for %r", query)
        return []


# ── yt-dlp audio download ─────────────────────────────────────────────────────

def download_audio(sc_url: str, tmpdir: str) -> tuple[str, dict]:
    """
    Download audio for *sc_url* (a soundcloud.com track URL) into *tmpdir*.

    Uses bestaudio/best.  If FFmpeg is present the file is remuxed to MP3 at
    192 kbps; otherwise the native format (usually mp3 from SoundCloud) is kept.

    Returns (file_path, yt_dlp_info_dict).
    Raises RuntimeError on any failure.
    """
    outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    ydl_opts: dict[str, Any] = {
        "format":        "bestaudio/best",
        "outtmpl":       outtmpl,
        "quiet":         False,
        "no_warnings":   False,
        "noplaylist":    True,
        "max_filesize":  MAX_FILE_MB * 1024 * 1024,
        "retries":       3,
        "fragment_retries": 3,
    }

    has_ffmpeg = _ffmpeg_available()
    if has_ffmpeg:
        ydl_opts["postprocessors"] = [{
            "key":              "FFmpegExtractAudio",
            "preferredcodec":   "mp3",
            "preferredquality": "192",
        }]
    else:
        logger.warning("FFmpeg not found — sending native audio format")

    info: dict = {}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(sc_url, download=True) or {}
    except yt_dlp.utils.DownloadError:
        logger.exception("yt-dlp DownloadError for %s", sc_url)
        raise RuntimeError(f"Download failed for: {sc_url}")
    except Exception:
        logger.exception("Unexpected error downloading %s", sc_url)
        raise RuntimeError(f"Unexpected download error for: {sc_url}")

    # Locate the output file
    track_id = info.get("id", "")
    path = ""

    if has_ffmpeg and track_id:
        mp3 = os.path.join(tmpdir, f"{track_id}.mp3")
        if os.path.exists(mp3):
            path = mp3

    if not path and info:
        try:
            with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
                path = ydl.prepare_filename(info)
                # prepare_filename gives the pre-postprocessor name; check .mp3 too
                if has_ffmpeg and not os.path.exists(path):
                    mp3 = os.path.splitext(path)[0] + ".mp3"
                    if os.path.exists(mp3):
                        path = mp3
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
            logger.error("tmpdir contents after failed locate: %s", os.listdir(tmpdir))
            raise RuntimeError("Download produced no output file.")
        path = os.path.join(tmpdir, candidates[0])

    size_mb = os.path.getsize(path) / (1024 * 1024)
    logger.info("Downloaded %s → %s (%.1f MB)", sc_url, path, size_mb)

    if size_mb > MAX_FILE_MB:
        raise RuntimeError(
            f"Track is {size_mb:.0f} MB — too large for Telegram (limit {MAX_FILE_MB} MB)."
        )

    return path, info


# ── UI builders ───────────────────────────────────────────────────────────────

def build_results_message(tracks: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    """Search-results list + inline keyboard. Each button opens the detail view."""
    lines: list[str] = [
        "<b>🎵 Search results</b>",
        "<i>Tap a track to see details and download it.</i>\n",
    ]
    buttons: list[list[InlineKeyboardButton]] = []

    for i, track in enumerate(tracks, start=1):
        title    = html.escape(track.get("title")    or "Unknown")
        uploader = html.escape(track.get("uploader") or "Unknown")
        dur      = fmt_duration(track.get("duration"))

        line = f"{i}. <b>{title}</b> — {uploader}"
        if dur:
            line += f"  ·  {dur}"
        lines.append(line)

        # callback_data ≤ 64 bytes; sc track IDs are short numeric strings
        tid   = str(track["id"])
        label = f"{i}. {track.get('title','')[:28]} — {track.get('uploader','')[:18]}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"info:{tid}")])

    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def build_track_detail(track: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Detail card for a single track + action keyboard."""
    title    = html.escape(track.get("title")       or "Unknown")
    uploader = html.escape(track.get("uploader")    or "Unknown")
    dur      = fmt_duration(track.get("duration"))
    url      = track.get("webpage_url") or track.get("url") or ""

    lines = [
        f"🎵 <b>{title}</b>",
        f"👤 {uploader}",
    ]
    if dur:
        lines.append(f"⏱ {dur}")
    if url:
        lines.append(f'\n<a href="{html.escape(url)}">Open on SoundCloud</a>')

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Download & Send Audio", callback_data=f"dl:{track['id']}")],
        [InlineKeyboardButton("🔙 Back to results",       callback_data="back")],
    ])
    return "\n".join(lines), keyboard


# ── Telegram command handlers ─────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text(
        "🎵 <b>Music Bot (SoundCloud)</b>\n\n"
        "Search SoundCloud and get the audio file sent straight to you.\n\n"
        "• Type any song name or <code>Song - Artist</code>\n"
        "• Paste a SoundCloud track URL to download it directly\n"
        "• Use /search for a step-by-step prompt\n"
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
    """
    If *query* is a SoundCloud URL download it immediately.
    Otherwise run a scsearch and show the results keyboard.
    """
    if not query:
        await msg.reply_text("Please send a song name.")
        return

    # Direct SoundCloud URL — skip search, go straight to download
    if _is_sc_url(query):
        status = await msg.reply_text("⬇️ Downloading audio — please wait…")
        await _download_and_send(
            sc_url=query,
            chat_id=msg.chat_id,
            status_msg=status,
            context=context,
        )
        return

    status = await msg.reply_text("🔍 Searching SoundCloud…")
    tracks = await _run_blocking(sc_search, query)

    if not tracks:
        await _safe_edit(
            status,
            f"No results for <b>{html.escape(query)}</b>. Try different words.",
        )
        return

    # Cache so the detail view and Back button can reuse without another search
    context.chat_data["last_tracks"] = tracks
    context.chat_data["last_query"]  = query

    text, keyboard = build_results_message(tracks)
    await _safe_edit(status, text, keyboard)


# ── Callback handler ──────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Single entry-point for all inline keyboard callbacks.

    Patterns:
        info:<track_id>  — show track detail card
        dl:<track_id>    — download and send audio
        back             — back to last results list
        cancel           — dismiss
    """
    cq = update.callback_query
    if not cq:
        return

    # Answer immediately — Telegram requires this within 10 seconds
    try:
        await cq.answer()
    except Exception:
        pass

    data = (cq.data or "").strip()

    # ── cancel ────────────────────────────────────────────────────────────────
    if data == "cancel":
        await _safe_edit(cq.message, "Cancelled. Type a song name to search again.")
        return

    # ── back to results ───────────────────────────────────────────────────────
    if data == "back":
        tracks = context.chat_data.get("last_tracks")
        if not tracks:
            await _safe_edit(cq.message, "Session expired — please search again.")
            return
        text, keyboard = build_results_message(tracks)
        await _safe_edit(cq.message, text, keyboard)
        return

    # ── track detail ──────────────────────────────────────────────────────────
    if data.startswith("info:"):
        track_id = data[5:]
        tracks   = context.chat_data.get("last_tracks") or []
        track    = next((t for t in tracks if str(t.get("id")) == track_id), None)

        if not track:
            await _safe_edit(cq.message, "❌ Track not found — please search again.")
            return

        text, keyboard = build_track_detail(track)
        thumb = track.get("thumbnail") or ""

        if thumb:
            try:
                await cq.message.reply_photo(
                    photo=thumb,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                await _safe_edit(
                    cq.message,
                    "⬆️ Track details above.",
                )
                return
            except Exception:
                logger.exception("reply_photo failed for track %s, falling back to text", track_id)

        await _safe_edit(cq.message, text, keyboard)
        return

    # ── download ──────────────────────────────────────────────────────────────
    if data.startswith("dl:"):
        track_id = data[3:]
        tracks   = context.chat_data.get("last_tracks") or []
        track    = next((t for t in tracks if str(t.get("id")) == track_id), None)

        sc_url = ""
        if track:
            sc_url = track.get("webpage_url") or track.get("url") or ""
        if not sc_url:
            # Reconstruct a best-guess URL from the numeric ID
            sc_url = f"https://api.soundcloud.com/tracks/{track_id}"

        await _safe_edit(cq.message, "⬇️ Downloading audio — this usually takes 10–30 seconds…")

        chat_id = cq.message.chat_id if cq.message else None
        if not chat_id:
            return

        await _download_and_send(
            sc_url=sc_url,
            chat_id=chat_id,
            status_msg=cq.message,
            context=context,
            track_meta=track,
        )
        return

    logger.warning("Unhandled callback data: %r", data)


# ── Core download-and-send helper ─────────────────────────────────────────────

async def _download_and_send(
    sc_url:     str,
    chat_id:    int,
    status_msg: Any,
    context:    ContextTypes.DEFAULT_TYPE,
    track_meta: dict | None = None,
) -> None:
    """Download *sc_url*, send the audio to *chat_id*, then delete the file."""
    tmpdir = tempfile.mkdtemp(prefix="scbot_", dir="/tmp")
    try:
        path, dl_info = await _run_blocking(download_audio, sc_url, tmpdir)

        # Prefer yt-dlp metadata; fall back to cached search metadata
        title    = dl_info.get("title")    or (track_meta or {}).get("title")    or ""
        artist   = dl_info.get("uploader") or (track_meta or {}).get("uploader") or ""
        dur_secs = dl_info.get("duration") or (track_meta or {}).get("duration")
        page_url = dl_info.get("webpage_url") or sc_url

        parts: list[str] = []
        if title:
            parts.append(f"🎵 <b>{html.escape(title)}</b>")
        if artist:
            parts.append(f"👤 {html.escape(artist)}")
        d = fmt_duration(int(dur_secs) if dur_secs else None)
        if d:
            parts.append(f"⏱ {d}")
        parts.append(f'\n<a href="{html.escape(page_url)}">Open on SoundCloud</a>')
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
            status_msg,
            "✅ Done! Type another song name or paste a SoundCloud URL to get more music.",
        )

    except RuntimeError as exc:
        logger.exception("RuntimeError in _download_and_send for %s", sc_url)
        await _safe_edit(status_msg, f"❌ {exc}")
    except Exception:
        logger.exception("Unexpected error in _download_and_send for %s", sc_url)
        await _safe_edit(
            status_msg,
            "❌ Something went wrong. Please try again or search for a different track.",
        )
    finally:
        # Always delete the temp directory — /tmp space is limited on Railway
        shutil.rmtree(tmpdir, ignore_errors=True)
        logger.info("Cleaned up tmpdir %s", tmpdir)


# ── Safe edit helper ──────────────────────────────────────────────────────────

async def _safe_edit(
    message: Any,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit a message, absorbing harmless Telegram errors."""
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
    """Run a blocking function in the default executor (thread pool)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


# ── Railway health-check HTTP server ─────────────────────────────────────────

def _start_health_server() -> None:
    """
    Minimal HTTP server on $PORT so Railway's health check passes.
    Runs in a daemon thread and does not interfere with Telegram polling.
    """
    port = int(os.environ.get("PORT", 8080))

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *args):
            pass  # silence HTTP access logs

    server = HTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health-check server listening on port %d", port)


# ── Global error handler ──────────────────────────────────────────────────────

async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled update error", exc_info=context.error)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    token = (
        os.environ.get("TELEGRAM_BOT_TOKEN")
        or os.environ.get("BOT_TOKEN")
    )
    if not token:
        raise SystemExit(
            "Set TELEGRAM_BOT_TOKEN (or BOT_TOKEN) to your BotFather token."
        )

    # ── startup diagnostics (always stdout) ──────────────────────────────────
    print(f"yt-dlp version  : {yt_dlp.version.__version__}", flush=True)
    print(f"FFmpeg available: {_ffmpeg_available()}", flush=True)
    logger.info("yt-dlp version  : %s", yt_dlp.version.__version__)
    logger.info("FFmpeg available: %s", _ffmpeg_available())
    # ─────────────────────────────────────────────────────────────────────────

    _start_health_server()

    app = Application.builder().token(token).build()

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
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_text))
    app.add_error_handler(handle_error)

    logger.info("Bot starting — polling for updates…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
