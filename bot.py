import os
import re
import json
import uuid
import time
import shutil
import sqlite3
import asyncio
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# Terminal logging configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Agar .env me define na ho to current directory me cookies.txt check karega
COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE", os.path.join(os.path.dirname(__file__), "cookies.txt"))
LOCAL_API_URL = os.getenv("TELEGRAM_LOCAL_API_URL")
DB_PATH = os.getenv("BOT_DB_PATH", "bot_data.db")

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Telegram's cloud Bot API hard-limits uploads to 50MB. Local Bot API allows ~2GB.
MAX_TELEGRAM_FILE_SIZE = (2000 if LOCAL_API_URL else 50) * 1024 * 1024

DOWNLOAD_TIMEOUT_SECONDS = 900
INFO_FETCH_TIMEOUT_SECONDS = 60

# Render's Web Service type requires binding to a port and receiving HTTP
# traffic to avoid sleeping after 15 min of inactivity. This tiny server just
# answers "OK" so Render's health check passes and an external uptime pinger
# (e.g. UptimeRobot) has something to hit periodically.
RENDER_PORT = int(os.getenv("PORT", "10000"))


class _HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive")

    def log_message(self, format, *args):
        pass  # keep this out of the terminal logs


def start_health_server():
    server = HTTPServer(("0.0.0.0", RENDER_PORT), _HealthCheckHandler)
    logger.info(f"🌐 Health-check server listening on port {RENDER_PORT}")
    server.serve_forever()

# Max downloads actually running (yt-dlp subprocess) at the same time.
# Extra requests wait in a queue instead of all hitting YouTube/the server at once.
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "5"))
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

FORMAT_OPTIONS = [
    ("🎵 MP3 (audio only)", "mp3"),
    ("🎬 MP4 (video)", "mp4"),
]

BITRATE_OPTIONS = [
    ("64 kbps (chhoti size)", "64K"),
    ("96 kbps", "96K"),
    ("128 kbps (balanced)", "128K"),
    ("192 kbps", "192K"),
    ("256 kbps", "256K"),
    ("320 kbps (best quality)", "320K"),
]

# height caps + a true uncapped "best" option (no artificial resolution ceiling)
VIDEO_QUALITY_OPTIONS = [
    ("360p (chhoti size)", "360"),
    ("480p", "480"),
    ("720p (HD)", "720"),
    ("1080p (Full HD)", "1080"),
    ("1440p (2K)", "1440"),
    ("2160p (4K)", "2160"),
    ("🌟 Best available (uncapped)", "best"),
]

MAX_YTDLP_RETRIES = 3
RETRYABLE_ERROR_MARKERS = (b"needs to be reloaded", b"HTTP Error 429")

ACTIVE_DOWNLOADS = {}

PROGRESS_RE = re.compile(
    r"\[download\]\s+(?P<percent>[\d.]+)%"
    r"(?:\s+of\s+~?\s*(?P<size>[\d.]+\w+))?"
    r"(?:\s+at\s+(?P<speed>[\d.]+\w+/s|Unknown speed))?"
    r"(?:\s+ETA\s+(?P<eta>[\d:]+|Unknown))?"
)

# YouTube hides some higher-quality formats from the format list entirely when
# a valid PO token isn't present, even though yt-dlp COULD still fetch them.
# This extractor-arg tells yt-dlp to include those formats anyway instead of
# silently falling back to a lower max resolution. See yt-dlp issue #12963.
YOUTUBE_EXTRACTOR_ARGS = "youtube:player_client=web,android;formats=missing_pot"


# ---------------------------------------------------------------------------
# Database helpers (SQLite)
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT,
            fmt TEXT,
            quality TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS preferences (
            user_id INTEGER PRIMARY KEY,
            default_format TEXT,
            default_audio_quality TEXT,
            default_video_quality TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def save_history(user_id, title, fmt, quality):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO downloads (user_id, title, fmt, quality) VALUES (?, ?, ?, ?)",
        (user_id, title, fmt, quality),
    )
    conn.commit()
    conn.close()


def get_history(user_id, limit=10):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT title, fmt, quality FROM downloads WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return rows


def get_preferences(user_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT default_format, default_audio_quality, default_video_quality "
        "FROM preferences WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"format": row[0], "audio_quality": row[1], "video_quality": row[2]}


def set_preference(user_id, **fields):
    conn = sqlite3.connect(DB_PATH)
    existing = conn.execute(
        "SELECT user_id FROM preferences WHERE user_id = ?", (user_id,)
    ).fetchone()
    if existing:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE preferences SET {set_clause} WHERE user_id = ?",
            (*fields.values(), user_id),
        )
    else:
        columns = ", ".join(["user_id"] + list(fields.keys()))
        placeholders = ", ".join(["?"] * (len(fields) + 1))
        conn.execute(
            f"INSERT INTO preferences ({columns}) VALUES ({placeholders})",
            (user_id, *fields.values()),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------

def format_duration(seconds):
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def build_progress_bar(percent, width=18):
    percent = max(0.0, min(100.0, percent))
    filled = int(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def parse_progress(line):
    match = PROGRESS_RE.search(line)
    if not match:
        return None
    return match.groupdict()


def common_ytdlp_flags(output_template):
    flags = [
        "--no-playlist",
        "--extractor-args", YOUTUBE_EXTRACTOR_ARGS,
        "--concurrent-fragments", "4",
        "--newline",
        "--no-warnings",
        "-o", output_template,
    ]
    if os.path.isfile(COOKIES_FILE):
        flags += ["--cookies", COOKIES_FILE]
    return flags


async def get_video_info(url):
    """Fetch title + duration without downloading."""
    command = [
        "yt-dlp", "-J", "--no-playlist", "--no-warnings",
        "--extractor-args", YOUTUBE_EXTRACTOR_ARGS,
    ]
    if os.path.isfile(COOKIES_FILE):
        command += ["--cookies", COOKIES_FILE]
    command.append(url)

    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=INFO_FETCH_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        logger.error("❌ Info fetch request timed out.")
        return None

    if process.returncode != 0:
        logger.error(f"❌ yt-dlp info error: {stderr.decode(errors='ignore')}")
        return None

    try:
        data = json.loads(stdout.decode(errors="ignore"))
    except json.JSONDecodeError:
        return None

    return {"title": data.get("title") or "Unknown", "duration": data.get("duration") or 0}


async def run_yt_dlp_with_progress(command, request_id, on_progress):
    full_output = b""
    returncode = 1

    for attempt in range(1, MAX_YTDLP_RETRIES + 1):
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        ACTIVE_DOWNLOADS[request_id] = {"process": process, "cancelled": False}
        attempt_output = b""

        async def read_stream():
            nonlocal attempt_output
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                attempt_output += line
                text = line.decode(errors="ignore").strip()
                progress = parse_progress(text)
                if progress:
                    await on_progress(progress)
                elif any(tag in text for tag in ("[ExtractAudio]", "[Merger]", "[VideoConvertor]", "[ffmpeg]")):
                    await on_progress({"status_text": "🔄 Merging & Processing..."})

        try:
            await asyncio.wait_for(read_stream(), timeout=DOWNLOAD_TIMEOUT_SECONDS)
            returncode = await process.wait()
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            ACTIVE_DOWNLOADS.pop(request_id, None)
            logger.error(f"❌ Download timed out for request: {request_id}")
            return 1, attempt_output, "timeout"

        full_output += attempt_output
        cancelled = ACTIVE_DOWNLOADS.get(request_id, {}).get("cancelled", False)
        ACTIVE_DOWNLOADS.pop(request_id, None)

        if cancelled:
            logger.info(f"🚫 Download cancelled for request: {request_id}")
            return returncode, full_output, "cancelled"

        if returncode == 0:
            return returncode, full_output, None

        is_retryable = any(marker in attempt_output for marker in RETRYABLE_ERROR_MARKERS)
        if is_retryable and attempt < MAX_YTDLP_RETRIES:
            logger.warning(f"⚠️ Retrying yt-dlp attempt {attempt}/{MAX_YTDLP_RETRIES}...")
            await on_progress({"status_text": f"⚠️ Retrying attempt ({attempt}/{MAX_YTDLP_RETRIES})..."})
            await asyncio.sleep(3 * attempt)
            continue

        break

    return returncode, full_output, None


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"User {update.effective_user.id} used /start")
    await update.message.reply_text(
        "👋 Hello!\n\n"
        "YouTube video ka link bhejo — main format aur quality pooch kar file send kar dunga.\n\n"
        "📚 /history — apni recent downloads dekho\n"
        "⚙️ /settings — default format/quality set karo"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 YouTube link bhejo, format (MP3/MP4) aur quality choose karo.\n\n"
        "/history — pichli downloads\n"
        "/settings — default format/quality set karo"
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prefs = get_preferences(update.effective_user.id) or {}
    fmt = prefs.get("format") or "Set nahi hai"
    audio_q = prefs.get("audio_quality") or "Set nahi hai"
    video_q = prefs.get("video_quality") or "Set nahi hai"

    text = (
        "⚙️ Settings\n\n"
        f"Default format: {fmt}\n"
        f"Audio quality: {audio_q}\n"
        f"Video quality: {video_q}\n\n"
        "Naya default set karne ke liye format choose karo:"
    )
    buttons = [
        [InlineKeyboardButton("🎵 MP3", callback_data="setfmt|mp3")],
        [InlineKeyboardButton("🎬 MP4", callback_data="setfmt|mp4")],
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_history(update.effective_user.id, limit=10)
    if not rows:
        await update.message.reply_text("📚 Abhi tak koi download history nahi hai.")
        return

    lines = ["📚 Your Recent Downloads\n"]
    for i, (title, fmt, quality) in enumerate(rows, start=1):
        icon = "🎵" if fmt == "mp3" else "🎬"
        title_display = (title or "Unknown")
        title_display = title_display[:40] + ("…" if len(title_display) > 40 else "")
        quality_label = quality if fmt == "mp3" else f"{quality}p"
        lines.append(f"{i}. {icon} {title_display}\n   {fmt.upper()} • {quality_label}")

    await update.message.reply_text("\n\n".join(lines))


# ---------------------------------------------------------------------------
# Link -> format -> quality flow
# ---------------------------------------------------------------------------

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    logger.info(f"Incoming URL from User {update.effective_user.id}: {url}")

    if "youtube.com" not in url and "youtu.be" not in url:
        await update.message.reply_text("❌ Please ek valid YouTube link bhejo.")
        return

    status_message = await update.message.reply_text("🔍 Video info fetch ho rahi hai...")

    info = await get_video_info(url)
    if not info:
        await status_message.edit_text(
            "❌ Video info nahi mil payi. Link check karein ya dobara try karein."
        )
        return

    request_id = uuid.uuid4().hex[:8]
    context.bot_data[f"url:{request_id}"] = url
    context.bot_data[f"title:{request_id}"] = info["title"]

    title_display = info["title"][:60] + ("…" if len(info["title"]) > 60 else "")
    duration_str = format_duration(info["duration"])

    buttons = [
        [InlineKeyboardButton(label, callback_data=f"fmt|{fmt}|{request_id}")]
        for label, fmt in FORMAT_OPTIONS
    ]

    prefs = get_preferences(update.effective_user.id)
    if prefs and prefs.get("format"):
        quick_quality = (
            prefs.get("audio_quality") if prefs["format"] == "mp3" else prefs.get("video_quality")
        )
        if quick_quality:
            quick_label = f"⚡ Quick: {prefs['format'].upper()} {quick_quality}"
            buttons.insert(
                0,
                [InlineKeyboardButton(
                    quick_label, callback_data=f"dl|{prefs['format']}|{quick_quality}|{request_id}"
                )],
            )

    text = (
        "🎬 Video Mila\n\n"
        f"Title: {title_display}\n"
        f"⏱ Duration: {duration_str}\n\n"
        "Format choose karo:"
    )

    await status_message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def handle_format_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, fmt, request_id = query.data.split("|")
    except ValueError:
        await query.edit_message_text("❌ Invalid selection.")
        return

    if f"url:{request_id}" not in context.bot_data:
        await query.edit_message_text(
            "❌ Yeh link expire ho gaya. Naya link bhejo."
        )
        return

    if fmt == "mp3":
        options, prompt = BITRATE_OPTIONS, "🎚️ Kis bitrate mein MP3 chahiye?"
    else:
        options, prompt = VIDEO_QUALITY_OPTIONS, "🎚️ Kis quality mein MP4 chahiye?"

    buttons = [
        [InlineKeyboardButton(label, callback_data=f"dl|{fmt}|{value}|{request_id}")]
        for label, value in options
    ]
    await query.edit_message_text(prompt, reply_markup=InlineKeyboardMarkup(buttons))


async def handle_quality_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, fmt, quality, request_id = query.data.split("|")
    except ValueError:
        await query.edit_message_text("❌ Invalid selection.")
        return

    url = context.bot_data.pop(f"url:{request_id}", None)
    title = context.bot_data.pop(f"title:{request_id}", None)
    if not url:
        await query.edit_message_text(
            "❌ Yeh link expire ho gaya. Naya link bhejo."
        )
        return

    logger.info(f"User {update.effective_user.id} selected {fmt.upper()} ({quality}) for '{title}'")
    message = await query.edit_message_text("⏳ Shuru ho raha hai...")
    await download_and_send(url, fmt, quality, message, request_id, update.effective_user.id, title)


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Cancelling...")

    try:
        _, request_id = query.data.split("|")
    except ValueError:
        return

    entry = ACTIVE_DOWNLOADS.get(request_id)
    if entry:
        entry["cancelled"] = True
        entry["process"].kill()
        logger.info(f"User manually cancelled request {request_id}")


# ---------------------------------------------------------------------------
# Settings flow
# ---------------------------------------------------------------------------

async def handle_set_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, fmt = query.data.split("|")

    if fmt == "mp3":
        options, prompt, prefix = BITRATE_OPTIONS, "🎚️ Default MP3 bitrate choose karo:", "setaudioq"
    else:
        options, prompt, prefix = VIDEO_QUALITY_OPTIONS, "🎚️ Default MP4 quality choose karo:", "setvideoq"

    buttons = [
        [InlineKeyboardButton(label, callback_data=f"{prefix}|{value}|{fmt}")]
        for label, value in options
    ]
    await query.edit_message_text(prompt, reply_markup=InlineKeyboardMarkup(buttons))


async def handle_set_quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prefix, value, fmt = query.data.split("|")

    user_id = update.effective_user.id
    if prefix == "setaudioq":
        set_preference(user_id, default_format=fmt, default_audio_quality=value)
    else:
        set_preference(user_id, default_format=fmt, default_video_quality=value)

    await query.edit_message_text(
        f"✅ Default set ho gaya: {fmt.upper()} — {value}\n\n"
        "Ab jab bhi link bhejoge, ek '⚡ Quick' button milega isi setting ke saath."
    )


# ---------------------------------------------------------------------------
# Download + send
# ---------------------------------------------------------------------------

async def download_and_send(url: str, fmt: str, quality: str, message, request_id: str, user_id: int, title):
    request_dir = os.path.join(DOWNLOAD_DIR, uuid.uuid4().hex)
    os.makedirs(request_dir, exist_ok=True)
    output_template = os.path.join(request_dir, "%(id)s.%(ext)s")

    if fmt == "mp3":
        command = [
            "yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", quality,
            *common_ytdlp_flags(output_template)
        ]
        target_ext = ".mp3"
    else:
        # "best" = no height restriction at all -> true source-max quality.
        # Otherwise cap at the chosen height, falling back to unrestricted
        # best if nothing matches that cap (rare).
        if quality == "best":
            format_selector = "bv*+ba/b"
        else:
            format_selector = f"bv*[height<={quality}]+ba/b[height<={quality}] / bv*+ba/b"

        command = [
            "yt-dlp",
            "-f", format_selector,
            "--format-sort", "res,fps,br,size",
            "--merge-output-format", "mp4",
            *common_ytdlp_flags(output_template)
        ]
        target_ext = ".mp4"

    command.append(url)

    cancel_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel|{request_id}")]]
    )

    state = {"last_edit_time": 0.0, "last_percent": -10.0}

    async def on_progress(data):
        now = time.monotonic()
        if "status_text" in data:
            try:
                await message.edit_text(data["status_text"], reply_markup=cancel_markup)
            except Exception:
                pass
            return

        try:
            percent = float(data.get("percent") or 0)
        except ValueError:
            return

        if now - state["last_edit_time"] < 3 and abs(percent - state["last_percent"]) < 2:
            return
        state["last_edit_time"] = now
        state["last_percent"] = percent

        bar = build_progress_bar(percent)
        speed = data.get("speed") or "—"
        eta = data.get("eta") or "—"
        text = f"⬇️ Downloading...\n\n{bar} {percent:.0f}%\n\nSpeed: {speed}\nETA: {eta}"
        try:
            await message.edit_text(text, reply_markup=cancel_markup)
        except Exception:
            pass

    try:
        try:
            await message.edit_text("⏳ Processing start ho rahi hai...", reply_markup=cancel_markup)
        except Exception:
            pass

        # If MAX_CONCURRENT_DOWNLOADS slots are all busy, this request waits here
        # instead of piling more load onto the server/YouTube all at once.
        try:
            await asyncio.wait_for(download_semaphore.acquire(), timeout=0.1)
        except asyncio.TimeoutError:
            try:
                await message.edit_text(
                    "🕓 Bot busy hai (bahut saare downloads chal rahe hain), queue mein wait kar rahe ho...",
                    reply_markup=cancel_markup,
                )
            except Exception:
                pass
            await download_semaphore.acquire()
            try:
                await message.edit_text("⏳ Processing start ho rahi hai...", reply_markup=cancel_markup)
            except Exception:
                pass

        logger.info(f"Running download pipeline for request {request_id}...")
        try:
            returncode, output, status = await run_yt_dlp_with_progress(command, request_id, on_progress)
        finally:
            download_semaphore.release()

        if status == "cancelled":
            try:
                await message.edit_text("❌ Cancel kar diya gaya.", reply_markup=None)
            except Exception:
                pass
            return

        files = [
            os.path.join(request_dir, f)
            for f in os.listdir(request_dir)
            if f.lower().endswith(target_ext)
        ]

        if not files:
            if status == "timeout":
                logger.error(f"Request {request_id} timed out before creating output file.")
                try:
                    await message.edit_text("❌ Timeout ho gaya, video bahut bada hai.", reply_markup=None)
                except Exception:
                    pass
            else:
                logger.error(f"YT-DLP ERROR LOGS:\n{output.decode(errors='ignore')}")
                try:
                    await message.edit_text("❌ Download fail ho gaya. Kripya doosra link try karein.", reply_markup=None)
                except Exception:
                    pass
            return

        output_file = files[0]
        file_size = os.path.getsize(output_file)
        size_mb = file_size / (1024 * 1024)
        logger.info(f"File ready on disk: {output_file} ({size_mb:.2f} MB)")

        if file_size > MAX_TELEGRAM_FILE_SIZE:
            suggestion = "kam bitrate (jaise 64K ya 96K)" if fmt == "mp3" else "kam resolution (jaise 360p ya 480p)"
            logger.warning(f"File ({size_mb:.2f} MB) exceeded Telegram limit.")
            try:
                await message.edit_text(
                    f"❌ File size ({size_mb:.1f} MB) allowed limit se badi hai. {suggestion} try karein.", reply_markup=None
                )
            except Exception:
                pass
            return

        try:
            await message.edit_text("📤 Telegram par upload ho raha hai...", reply_markup=None)
        except Exception:
            pass

        file_title = title or os.path.basename(output_file)[: -len(target_ext)]
        logger.info(f"Uploading file to Telegram for user {user_id}...")

        # --- SAFE UPLOAD BLOCK ---
        is_high_res = fmt == "mp4" and (quality == "best" or (quality.isdigit() and int(quality) >= 1080))

        with open(output_file, "rb") as media:
            if fmt == "mp3":
                await message.chat.send_audio(
                    audio=media,
                    title=file_title,
                    read_timeout=600,
                    write_timeout=600,
                    connect_timeout=60
                )
            else:
                if is_high_res:
                    await message.chat.send_document(
                        document=media,
                        caption=f"🎬 {file_title}",
                        read_timeout=600,
                        write_timeout=600,
                        connect_timeout=60
                    )
                else:
                    await message.chat.send_video(
                        video=media,
                        caption=file_title,
                        supports_streaming=True,
                        read_timeout=600,
                        write_timeout=600,
                        connect_timeout=60
                    )

        logger.info(f"✅ Upload completed successfully for User {user_id}: {file_title}")

        if fmt == "mp3":
            quality_label = quality
        elif quality == "best":
            quality_label = "Best Available"
        else:
            quality_label = f"{quality}p"

        congrats_text = (
            "🎉 Congratulations! Aapki file taiyar hai!\n\n"
            f"{'🎵' if fmt == 'mp3' else '🎬'} {file_title}\n"
            f"📦 {fmt.upper()} • {quality_label}\n"
            f"💾 {size_mb:.1f} MB"
        )
        try:
            await message.chat.send_message(congrats_text)
        except Exception as congrats_err:
            logger.error(f"Failed to send congratulations message: {congrats_err}")

        try:
            await message.delete()
        except Exception:
            try:
                await message.edit_text("✅ Complete!", reply_markup=None)
            except Exception:
                pass

        try:
            save_history(user_id, file_title, fmt, quality)
        except Exception as db_err:
            logger.error(f"Database error: {db_err}")

    except Exception as e:
        logger.error(f"CRITICAL BOT ERROR: {repr(e)}", exc_info=True)
        try:
            await message.edit_text("❌ Download/Upload error aa gaya.", reply_markup=None)
        except Exception:
            pass
    finally:
        shutil.rmtree(request_dir, ignore_errors=True)
        ACTIVE_DOWNLOADS.pop(request_id, None)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is missing from .env")

    init_db()

    if os.path.isfile(COOKIES_FILE):
        logger.info(f"✅ Cookies file found: {COOKIES_FILE}")
    else:
        logger.warning(f"⚠️ Cookies file NOT found at: {COOKIES_FILE} — bot will run WITHOUT cookies")

    # Runs in a background thread so it doesn't interfere with the bot's own event loop
    threading.Thread(target=start_health_server, daemon=True).start()

    builder = (
        Application.builder()
        .token(TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(60)
        .concurrent_updates(MAX_CONCURRENT_DOWNLOADS + 5)  # allow multiple users' messages to be handled at once
    )
    if LOCAL_API_URL:
        builder = builder.base_url(f"{LOCAL_API_URL}/bot").base_file_url(f"{LOCAL_API_URL}/file/bot")
        logger.info(f"📡 Using Local Bot API Server at {LOCAL_API_URL} (up to 2GB uploads)")
    else:
        logger.info("☁️ Using Telegram cloud API (50MB upload limit)")

    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("history", history_command))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    app.add_handler(CallbackQueryHandler(handle_format_choice, pattern=r"^fmt\|"))
    app.add_handler(CallbackQueryHandler(handle_quality_choice, pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(handle_cancel, pattern=r"^cancel\|"))
    app.add_handler(CallbackQueryHandler(handle_set_format, pattern=r"^setfmt\|"))
    app.add_handler(CallbackQueryHandler(handle_set_quality, pattern=r"^set(?:audio|video)q\|"))

    logger.info("🤖 Bot is active and listening for messages...")
    app.run_polling(drop_pending_updates=True, timeout=30)


if __name__ == "__main__":
    main()