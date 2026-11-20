"""
Telegram Music Bot
- Searches SoundCloud via yt-dlp scsearch
- Downloads audio via yt-dlp (bestaudio/best → FFmpegExtractAudio → mp3)
- Sends the mp3 to the user then deletes the file from /tmp

Design rule: every callback always operates on the SAME message (cq.message).
We never reply_photo or send new messages from a callback, because that breaks
subsequent callbacks whose cq.message would be the wrong object.
"""

import asyncio
import functools
import html
import json
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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)

# Silence noisy libraries that would leak the bot token or flood the log.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.vendor.ptb_urllib3").setLevel(logging.WARNING)


# ── yt-dlp logger adapter ─────────────────────────────────────────────────────

class _YtdlLogger:
    """Routes yt-dlp internal messages into Python's logging system."""

    def debug(self, msg: str) -> None:
        if "[download]" in msg:   # skip noisy progress lines
            return
        logger.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        logger.info("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        logger.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        logger.error("yt-dlp: %s", msg)


_YTDL_LOGGER = _YtdlLogger()

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_RESULTS = 5
MAX_FILE_MB = 48    # Telegram bot file upload limit is 50 MB
ASK_SONG    = 0     # ConversationHandler state

# ── Error messages shown to users (include code for log matching) ─────────────

_ERR: dict[str, str] = {
    "DOWNLOAD_FAILED": (
        "❌ Download failed. The track may be unavailable or region-locked.\n"
        "<code>[DOWNLOAD_FAILED]</code>"
    ),
    "FILE_TOO_LARGE": (
        f"❌ That track exceeds Telegram's {MAX_FILE_MB} MB upload limit.\n"
        "<code>[FILE_TOO_LARGE]</code>"
    ),
    "SEND_FAILED": (
        "❌ The file downloaded but Telegram refused the upload.\n"
        "<code>[SEND_FAILED]</code>"
    ),
    "SEARCH_EMPTY": (
        "No results found. Try different words.\n"
        "<code>[SEARCH_EMPTY]</code>"
    ),
    "INTERNAL_ERROR": (
        "❌ An unexpected error occurred. Please try again.\n"
        "<code>[INTERNAL_ERROR]</code>"
    ),
}


# ── Utility helpers ───────────────────────────────────────────────────────────

def fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))


def _is_sc_url(text: str) -> bool:
    """True when *text* is a soundcloud.com track URL."""
    try:
        p = urlparse(text.strip())
        return p.scheme in ("http", "https") and "soundcloud.com" in p.netloc
    except Exception:
        return False


# ── SoundCloud search ─────────────────────────────────────────────────────────

def sc_search(query: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    """
    Search SoundCloud with yt-dlp scsearch.
    Returns [] on failure (logs full traceback).
    """
    search_url = f"scsearch{limit}:{query}"
    logger.info("SEARCH_START query=%r source=SoundCloud url=%r", query, search_url)

    opts: dict[str, Any] = {
        "quiet":        False,
        "no_warnings":  False,
        "extract_flat": True,
        "noplaylist":   False,
        "logger":       _YTDL_LOGGER,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_url, download=False) or {}

        entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
        logger.info("SEARCH_DONE results=%d query=%r", len(entries), query)
        return entries

    except Exception:
        logger.exception("SEARCH_FAILED query=%r", query)
        return []


# ── yt-dlp audio download ─────────────────────────────────────────────────────

def download_audio(sc_url: str, tmpdir: str) -> tuple[str, dict]:
    """
    Download *sc_url* into *tmpdir*, remux to MP3 if FFmpeg is available.
    Returns (absolute_file_path, yt_dlp_info_dict).
    Raises RuntimeError("DOWNLOAD_FAILED" | "FILE_TOO_LARGE") on failure.
    """
    logger.info("DOWNLOAD_START url=%r tmpdir=%r", sc_url, tmpdir)

    has_ffmpeg = _ffmpeg_available()
    logger.info("DOWNLOAD ffmpeg=%s", has_ffmpeg)

    outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")
    opts: dict[str, Any] = {
        "format":           "bestaudio/best",
        "outtmpl":          outtmpl,
        "quiet":            False,
        "no_warnings":      False,
        "noplaylist":       True,
        "max_filesize":     MAX_FILE_MB * 1024 * 1024,
        "retries":          3,
        "fragment_retries": 3,
        "logger":           _YTDL_LOGGER,
    }

    if has_ffmpeg:
        opts["postprocessors"] = [{
            "key":              "FFmpegExtractAudio",
            "preferredcodec":   "mp3",
            "preferredquality": "192",
        }]
    else:
        logger.warning("DOWNLOAD ffmpeg_missing — native format will be sent")

    info: dict = {}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(sc_url, download=True) or {}
    except yt_dlp.utils.DownloadError:
        logger.exception("DOWNLOAD_FAILED [DownloadError] url=%r", sc_url)
        raise RuntimeError("DOWNLOAD_FAILED")
    except Exception:
        logger.exception("DOWNLOAD_FAILED [unexpected] url=%r", sc_url)
        raise RuntimeError("DOWNLOAD_FAILED")

    # Locate the produced file
    track_id = str(info.get("id") or "")
    path = ""

    if has_ffmpeg and track_id:
        mp3 = os.path.join(tmpdir, f"{track_id}.mp3")
        if os.path.exists(mp3):
            path = mp3
            logger.info("DOWNLOAD file_found via id+ext path=%r", path)

    if not path and info:
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "logger": _YTDL_LOGGER}) as ydl:
                raw = ydl.prepare_filename(info)
            for candidate in [raw, os.path.splitext(raw)[0] + ".mp3"]:
                if os.path.exists(candidate):
                    path = candidate
                    logger.info("DOWNLOAD file_found via prepare_filename path=%r", path)
                    break
        except Exception:
            logger.exception("DOWNLOAD prepare_filename error")

    if not path or not os.path.exists(path):
        all_files = [
            f for f in os.listdir(tmpdir)
            if not f.endswith(".part") and not f.endswith(".ytdl")
        ]
        logger.info("DOWNLOAD tmpdir_scan files=%r", all_files)
        if not all_files:
            logger.error("DOWNLOAD_FAILED no_output_file tmpdir=%r", tmpdir)
            raise RuntimeError("DOWNLOAD_FAILED")
        # Pick the largest file (most likely the audio)
        all_files.sort(
            key=lambda f: os.path.getsize(os.path.join(tmpdir, f)),
            reverse=True,
        )
        path = os.path.join(tmpdir, all_files[0])
        logger.info("DOWNLOAD file_found via scan path=%r", path)

    size_mb = os.path.getsize(path) / (1024 * 1024)
    logger.info("DOWNLOAD_COMPLETE path=%r size_mb=%.2f", path, size_mb)

    if size_mb > MAX_FILE_MB:
        logger.error("FILE_TOO_LARGE size_mb=%.2f url=%r", size_mb, sc_url)
        raise RuntimeError("FILE_TOO_LARGE")

    return path, info


# ── UI builders ───────────────────────────────────────────────────────────────

def build_results_message(tracks: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    """Results list as a single editable text message + inline keyboard."""
    lines: list[str] = [
        "<b>🎵 Search results — SoundCloud</b>",
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

        tid   = str(track["id"])
        # Button label must fit in 64 bytes for callback_data safety
        label = f"{i}. {track.get('title','')[:26]} — {track.get('uploader','')[:16]}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"info:{tid}")])

    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def build_track_detail(track: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Detail card for a single track — plain text only, no photo preview."""
    title    = html.escape(track.get("title")    or "Unknown")
    uploader = html.escape(track.get("uploader") or "Unknown")
    dur      = fmt_duration(track.get("duration"))
    sc_url   = track.get("webpage_url") or track.get("url") or ""

    lines: list[str] = [
        f"🎵 <b>{title}</b>",
        f"👤 {uploader}",
    ]
    if dur:
        lines.append(f"⏱ {dur}")
    if sc_url:
        lines.append(f'\n🔗 <a href="{html.escape(sc_url)}">Open on SoundCloud</a>')

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Download & Send Audio", callback_data=f"dl:{track['id']}")],
        [InlineKeyboardButton("🔙 Back to results",       callback_data="back")],
        [InlineKeyboardButton("❌ Cancel",                callback_data="cancel")],
    ])
    return "\n".join(lines), keyboard


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text(
        "🎵 <b>Music Bot — SoundCloud</b>\n\n"
        "Search SoundCloud and get the audio file sent straight to you.\n\n"
        "• Type any song name or <code>Song - Artist</code>\n"
        "• Paste a SoundCloud track URL to download directly\n"
        "• /search — step-by-step search prompt\n"
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
        await msg.reply_text("Cancelled. Send a song name whenever you're ready.")
    return ConversationHandler.END


async def handle_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return
    await _do_search(msg, msg.text.strip(), context)


# ── Search flow ───────────────────────────────────────────────────────────────

async def _do_search(
    msg: Any,
    query: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not query:
        await msg.reply_text("Please send a song name.")
        return

    logger.info("QUERY_RECEIVED chat_id=%s query=%r", msg.chat_id, query)

    # Direct SoundCloud URL — skip search
    if _is_sc_url(query):
        logger.info("DIRECT_URL url=%r", query)
        status = await msg.reply_text("⬇️ Downloading — please wait…")
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
        logger.warning("SEARCH_EMPTY query=%r", query)
        await _safe_edit(
            status,
            f"No results for <b>{html.escape(query)}</b>. "
            f"Try different words.\n<code>[SEARCH_EMPTY]</code>",
        )
        return

    context.chat_data["last_tracks"] = tracks
    context.chat_data["last_query"]  = query

    text, keyboard = build_results_message(tracks)
    await _safe_edit(status, text, keyboard)


# ── Callback handler ──────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Single handler for all inline keyboard callbacks.
    IMPORTANT: every branch edits cq.message in-place (never sends new messages)
    so subsequent button taps keep working on the same message object.
    """
    cq = update.callback_query
    if not cq or not cq.message:
        return

    # Must answer within 10 s or Telegram shows an error spinner.
    try:
        await cq.answer()
    except Exception:
        pass

    data     = (cq.data or "").strip()
    chat_id  = cq.message.chat_id
    logger.info("CALLBACK data=%r chat_id=%s", data, chat_id)

    # ── cancel ────────────────────────────────────────────────────────────────
    if data == "cancel":
        logger.info("CANCEL chat_id=%s", chat_id)
        await _safe_edit(
            cq.message,
            "Cancelled. Send a song name whenever you're ready.",
        )
        return

    # ── back to results ───────────────────────────────────────────────────────
    if data == "back":
        tracks = context.chat_data.get("last_tracks")
        if not tracks:
            logger.warning("BACK session_expired chat_id=%s", chat_id)
            await _safe_edit(
                cq.message,
                "Session expired — please search again.",
            )
            return
        logger.info("BACK restoring results chat_id=%s", chat_id)
        text, keyboard = build_results_message(tracks)
        await _safe_edit(cq.message, text, keyboard)
        return

    # ── track detail ──────────────────────────────────────────────────────────
    if data.startswith("info:"):
        track_id = data[5:]
        tracks   = context.chat_data.get("last_tracks") or []
        track    = next((t for t in tracks if str(t.get("id")) == track_id), None)

        if not track:
            logger.warning("TRACK_NOT_FOUND track_id=%r chat_id=%s", track_id, chat_id)
            await _safe_edit(
                cq.message,
                "❌ Track not found — please search again.",
            )
            return

        logger.info("TRACK_DETAIL track_id=%r title=%r", track_id, track.get("title"))
        text, keyboard = build_track_detail(track)
        # Edit the same message in-place — no new messages, no broken callbacks.
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
            # Fallback: reconstruct from numeric ID
            sc_url = f"https://soundcloud.com/tracks/{track_id}"

        logger.info("DOWNLOAD_REQUEST track_id=%r url=%r chat_id=%s",
                    track_id, sc_url, chat_id)

        await _safe_edit(
            cq.message,
            "⬇️ Downloading audio — this usually takes 10–30 seconds…",
        )

        await _download_and_send(
            sc_url=sc_url,
            chat_id=chat_id,
            status_msg=cq.message,
            context=context,
            track_meta=track,
        )
        return

    logger.warning("CALLBACK_UNHANDLED data=%r chat_id=%s", data, chat_id)


# ── Download-and-send helper ──────────────────────────────────────────────────

async def _download_and_send(
    sc_url:     str,
    chat_id:    int,
    status_msg: Any,
    context:    ContextTypes.DEFAULT_TYPE,
    track_meta: dict | None = None,
) -> None:
    """Download *sc_url*, send the audio to *chat_id*, delete the temp dir."""
    tmpdir = tempfile.mkdtemp(prefix="scbot_", dir="/tmp")
    logger.info("TMPDIR_CREATED %r", tmpdir)

    try:
        path, dl_info = await _run_blocking(download_audio, sc_url, tmpdir)

        title    = dl_info.get("title")    or (track_meta or {}).get("title")    or ""
        artist   = dl_info.get("uploader") or (track_meta or {}).get("uploader") or ""
        dur_secs = dl_info.get("duration") or (track_meta or {}).get("duration")
        page_url = dl_info.get("webpage_url") or sc_url
        size_mb  = os.path.getsize(path) / (1024 * 1024)

        logger.info(
            "UPLOAD_START chat_id=%s title=%r artist=%r size_mb=%.2f",
            chat_id, title, artist, size_mb,
        )

        parts: list[str] = []
        if title:
            parts.append(f"🎵 <b>{html.escape(title)}</b>")
        if artist:
            parts.append(f"👤 {html.escape(artist)}")
        dur_str = fmt_duration(int(dur_secs) if dur_secs else None)
        if dur_str:
            parts.append(f"⏱ {dur_str}")
        parts.append(f'\n🔗 <a href="{html.escape(page_url)}">Open on SoundCloud</a>')
        caption = "\n".join(parts)

        try:
            await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
            with open(path, "rb") as f:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=f,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    title=title   or None,
                    performer=artist or None,
                    duration=int(dur_secs) if dur_secs else None,
                )
            logger.info("UPLOAD_COMPLETE chat_id=%s title=%r size_mb=%.2f",
                        chat_id, title, size_mb)
        except Exception:
            logger.exception(
                "SEND_FAILED chat_id=%s title=%r size_mb=%.2f",
                chat_id, title, size_mb,
            )
            await _safe_edit(status_msg, _ERR["SEND_FAILED"])
            return

        await _safe_edit(
            status_msg,
            "✅ Sent! Type another song name or paste a SoundCloud URL for more.",
        )

    except RuntimeError as exc:
        code = str(exc)
        logger.exception("RUNTIME_ERROR code=%r url=%r", code, sc_url)
        await _safe_edit(status_msg, _ERR.get(code, _ERR["INTERNAL_ERROR"]))

    except Exception:
        logger.exception("UNEXPECTED_ERROR url=%r", sc_url)
        await _safe_edit(status_msg, _ERR["INTERNAL_ERROR"])

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        logger.info("TMPDIR_DELETED %r", tmpdir)


# ── Safe in-place message edit ────────────────────────────────────────────────

async def _safe_edit(
    message: Any,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """
    Edit *message* text in-place.
    Swallows harmless Telegram errors; logs everything else.
    """
    try:
        await message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        err = str(exc).lower()
        if "message is not modified" in err:
            pass   # identical content — not an error
        elif "there is no text in the message" in err:
            # cq.message is a photo/document — edit its caption instead
            try:
                await message.edit_caption(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            except Exception:
                logger.exception("EDIT_CAPTION_FAILED")
        else:
            logger.warning("EDIT_FAILED [BadRequest]: %s", exc)
    except (RetryAfter, TimedOut) as exc:
        logger.warning("EDIT_FAILED [rate-limit/timeout]: %s", exc)
    except Exception as exc:
        logger.warning("EDIT_FAILED [unexpected]: %s", exc)


# ── Thread-pool helper ────────────────────────────────────────────────────────

async def _run_blocking(fn: Any, *args: Any, **kwargs: Any) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


# ── Railway health-check server ───────────────────────────────────────────────

def _start_health_server() -> None:
    port = int(os.environ.get("PORT", 8080))

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *args):
            pass   # silence HTTP access log

    server = HTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("HEALTH_SERVER port=%d", port)


# ── Global Telegram error handler ────────────────────────────────────────────

async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the full traceback and the update that triggered it."""
    update_str = "no_update"
    if isinstance(update, Update):
        try:
            update_str = json.dumps(update.to_dict(), ensure_ascii=False)
        except Exception:
            update_str = repr(update)
    logger.exception(
        "UNHANDLED_ERROR update=%s",
        update_str,
        exc_info=context.error,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    token = (
        os.environ.get("TELEGRAM_BOT_TOKEN")
        or os.environ.get("BOT_TOKEN")
    )
    if not token:
        raise SystemExit(
            "Set TELEGRAM_BOT_TOKEN (or BOT_TOKEN) to your BotFather token."
        )

    print(f"yt-dlp version  : {yt_dlp.version.__version__}", flush=True)
    print(f"FFmpeg available: {_ffmpeg_available()}", flush=True)
    logger.info("STARTUP yt_dlp=%s ffmpeg=%s",
                yt_dlp.version.__version__, _ffmpeg_available())

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

    logger.info("STARTUP polling for updates…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
# redeploy
