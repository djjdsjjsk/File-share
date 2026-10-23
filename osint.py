import os
import re
import time
import uuid
import sqlite3
import logging
from pathlib import Path
from threading import Lock

import telebot
from telebot import types

# ============================================================
# TELEGRAM FILE STORE BOT - Render + SQLite
# Required environment variables:
# BOT_TOKEN, ADMIN_ID, CHANNEL_ID
# Optional:
# DATABASE_PATH (default /var/data/files.db)
# PUBLIC_BASE_URL is not required; Telegram deep links are used.
# ============================================================

BOT_TOKEN = os.getenv("8822909409:AAGm0buchw5lbqPPIj5QIPo9viXOIY_i-5o", "").strip()
ADMIN_ID_RAW = os.getenv("5989928539", "").strip()
CHANNEL_ID_RAW = os.getenv("-1004484156741", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "/var/data/files.db").strip()

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable.")
if not ADMIN_ID_RAW:
    raise RuntimeError("Missing ADMIN_ID environment variable.")
if not CHANNEL_ID_RAW:
    raise RuntimeError("Missing CHANNEL_ID environment variable.")

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
    CHANNEL_ID = int(CHANNEL_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ADMIN_ID and CHANNEL_ID must be numeric IDs.") from exc

# Make sure the database folder exists. On Render, mount a persistent disk
# at /var/data. For local testing, set DATABASE_PATH=files.db.
db_parent = Path(DATABASE_PATH).expanduser().resolve().parent
db_parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("telegram-file-store")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)
db_lock = Lock()

# Temporary upload queue: user_id -> source message_id and metadata.
# If the process restarts before Upload Now is clicked, ask the user to resend.
pending_uploads = {}


def db_connect():
    conn = sqlite3.connect(DATABASE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    with db_lock:
        with db_connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stored_files (
                    token TEXT PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    channel_message_id INTEGER NOT NULL,
                    owner_user_id INTEGER NOT NULL,
                    owner_name TEXT,
                    original_message_id INTEGER,
                    file_name TEXT,
                    media_type TEXT,
                    created_at INTEGER NOT NULL,
                    downloads INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_stored_files_created
                ON stored_files(created_at)
            """)
            conn.commit()


def html_escape(value):
    value = str(value or "")
    return (value.replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;")
                 .replace('"', "&quot;"))


def get_media_info(message):
    """Return (media_type, best-effort filename) for supported Telegram media."""
    if message.document:
        return "document", message.document.file_name or "document"
    if message.video:
        return "video", message.video.file_name or "video"
    if message.audio:
        return "audio", message.audio.file_name or message.audio.title or "audio"
    if message.animation:
        return "animation", message.animation.file_name or "animation"
    if message.voice:
        return "voice", "voice message"
    if message.video_note:
        return "video_note", "video note"
    if message.sticker:
        return "sticker", message.sticker.emoji or "sticker"
    if message.photo:
        return "photo", "photo"
    if message.contact:
        return "contact", "contact"
    if message.location:
        return "location", "location"
    if message.venue:
        return "venue", message.venue.title or "venue"
    return None, None


def make_upload_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton(
        "⬆️ Upload Now",
        callback_data="upload_now"
    ))
    return keyboard


def get_bot_link(token):
    username = bot.get_me().username
    return f"https://t.me/{username}?start={token}"


def get_saved_file(token):
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM stored_files WHERE token = ?",
            (token,)
        ).fetchone()


def save_file_record(token, channel_message_id, owner_id, owner_name,
                     original_message_id, file_name, media_type):
    with db_lock:
        with db_connect() as conn:
            conn.execute("""
                INSERT INTO stored_files (
                    token, channel_id, channel_message_id, owner_user_id,
                    owner_name, original_message_id, file_name, media_type,
                    created_at, downloads
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (
                token, CHANNEL_ID, channel_message_id, owner_id, owner_name,
                original_message_id, file_name, media_type, int(time.time())
            ))
            conn.commit()


def increment_downloads(token):
    with db_lock:
        with db_connect() as conn:
            conn.execute(
                "UPDATE stored_files SET downloads = downloads + 1 WHERE token = ?",
                (token,)
            )
            conn.commit()


@bot.message_handler(commands=["start"])
def start_handler(message):
    args = (message.text or "").split(maxsplit=1)
    payload = args[1].strip() if len(args) > 1 else ""

    if payload:
        # Tokens are generated by this bot and contain only safe characters.
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", payload):
            bot.reply_to(message, "❌ This share link is invalid.")
            return

        record = get_saved_file(payload)
        if not record:
            bot.reply_to(
                message,
                "❌ <b>File not found</b>\nThis link may be invalid or the file is no longer registered."
            )
            return

        try:
            # Copy the original channel post, preserving its media/caption as
            # Telegram stores it. No download/re-upload conversion is done.
            bot.copy_message(
                chat_id=message.chat.id,
                from_chat_id=record["channel_id"],
                message_id=record["channel_message_id"]
            )
            increment_downloads(payload)
            logger.info("Delivered token=%s to user_id=%s", payload, message.from_user.id)
        except telebot.apihelper.ApiTelegramException:
            logger.exception("Could not copy channel message for token=%s", payload)
            bot.send_message(
                message.chat.id,
                "⚠️ I couldn't send this file right now. Please contact the bot admin."
            )
        except Exception:
            logger.exception("Unexpected delivery error for token=%s", payload)
            bot.send_message(
                message.chat.id,
                "⚠️ Delivery failed temporarily. Please try again later."
            )
        return

    bot.reply_to(
        message,
        "📁 <b>Welcome to File Store Bot</b>\n\n"
        "Send me a file, photo, video, audio, sticker, or another supported "
        "Telegram media message.\n\n"
        "Then tap <b>⬆️ Upload Now</b> to save it and receive a shareable link."
    )


@bot.message_handler(commands=["id"])
def id_handler(message):
    """Useful for setup: returns the user's ID and current chat ID."""
    bot.reply_to(
        message,
        f"Your user ID: <code>{message.from_user.id}</code>\n"
        f"Current chat ID: <code>{message.chat.id}</code>"
    )


@bot.message_handler(content_types=[
    "document", "photo", "video", "audio", "voice",
    "animation", "sticker", "video_note", "contact",
    "location", "venue"
])
def media_handler(message):
    media_type, file_name = get_media_info(message)
    if not media_type:
        bot.reply_to(message, "⚠️ I couldn't identify that media. Please try another file.")
        return

    pending_uploads[message.from_user.id] = {
        "chat_id": message.chat.id,
        "message_id": message.message_id,
        "file_name": file_name,
        "media_type": media_type,
        "created_at": time.time(),
        "busy": False,
    }

    bot.reply_to(
        message,
        "📄 <b>File received</b>\n"
        f"🏷 <b>Name:</b> {html_escape(file_name)}\n"
        f"📦 <b>Type:</b> {html_escape(media_type)}\n\n"
        "Tap below to save it to the private channel.",
        reply_markup=make_upload_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data == "upload_now")
def upload_now_handler(call):
    user_id = call.from_user.id
    pending = pending_uploads.get(user_id)

    if not pending:
        bot.answer_callback_query(
            call.id,
            "No pending file found. Please send the file again.",
            show_alert=True
        )
        return

    if pending.get("busy"):
        bot.answer_callback_query(call.id, "Your file is already being saved.")
        return

    # Ignore stale pending uploads after 2 hours.
    if time.time() - pending.get("created_at", 0) > 7200:
        pending_uploads.pop(user_id, None)
        bot.answer_callback_query(
            call.id,
            "This upload expired. Please send the file again.",
            show_alert=True
        )
        return

    pending["busy"] = True
    bot.answer_callback_query(call.id, "Saving your file…")

    user = call.from_user
    owner_name = " ".join(
        part for part in [user.first_name, user.last_name] if part
    ).strip() or user.username or str(user_id)

    token = uuid.uuid4().hex[:16]

    try:
        # Copy from the user's chat to the configured private channel.
        # This preserves the Telegram media and its original caption.
        copied = bot.copy_message(
            chat_id=CHANNEL_ID,
            from_chat_id=pending["chat_id"],
            message_id=pending["message_id"]
        )

        channel_message_id = copied.message_id
        save_file_record(
            token=token,
            channel_message_id=channel_message_id,
            owner_id=user_id,
            owner_name=owner_name,
            original_message_id=pending["message_id"],
            file_name=pending["file_name"],
            media_type=pending["media_type"]
        )

        link = get_bot_link(token)

        # Add a separate reply-tag in the private channel so the original
        # copied media remains unchanged while its share link is easy to find.
        try:
            bot.send_message(
                CHANNEL_ID,
                "🏷 <b>FILE STORE RECORD</b>\n"
                f"📄 <b>File:</b> {html_escape(pending['file_name'])}\n"
                f"📦 <b>Type:</b> {html_escape(pending['media_type'])}\n"
                f"🆔 <b>File ID:</b> <code>{token}</code>\n"
                f"👤 <b>Uploaded by:</b> {html_escape(owner_name)} "
                f"(<code>{user_id}</code>)\n"
                f"🔗 <b>Share link:</b> {html_escape(link)}",
                reply_to_message_id=channel_message_id,
                disable_web_page_preview=True
            )
        except Exception:
            logger.exception("Could not add the file tag reply in the private channel")

        bot.send_message(
            pending["chat_id"],
            "✅ <b>File saved successfully!</b>\n\n"
            f"🏷 <b>File:</b> {html_escape(pending['file_name'])}\n"
            f"🆔 <b>File ID:</b> <code>{token}</code>\n\n"
            "🔗 <b>Shareable link:</b>\n"
            f"<code>{html_escape(link)}</code>\n\n"
            "Anyone with this link can open the bot and receive this file.",
            reply_to_message_id=pending["message_id"],
            disable_web_page_preview=True
        )

        # Add a labelled record in the admin chat for easy identification.
        # The actual stored media is the post in the private channel.
        if ADMIN_ID != user_id:
            try:
                bot.send_message(
                    ADMIN_ID,
                    "🗂 <b>New file stored</b>\n"
                    f"🏷 <b>Name:</b> {html_escape(pending['file_name'])}\n"
                    f"📦 <b>Type:</b> {html_escape(pending['media_type'])}\n"
                    f"👤 <b>Uploader:</b> {html_escape(owner_name)}\n"
                    f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
                    f"🆔 <b>File ID:</b> <code>{token}</code>\n"
                    f"📌 <b>Channel message ID:</b> <code>{channel_message_id}</code>\n"
                    f"🔗 {html_escape(link)}",
                    disable_web_page_preview=True
                )
            except Exception:
                logger.exception("Could not send admin notification")

        pending_uploads.pop(user_id, None)

        # Update the button message to show completion.
        try:
            bot.edit_message_text(
                "✅ <b>Upload complete.</b> Your shareable link has been sent above.",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id
            )
        except Exception:
            pass

    except telebot.apihelper.ApiTelegramException as exc:
        logger.exception("Telegram API error while saving file")
        pending_uploads[user_id]["busy"] = False
        bot.send_message(
            pending["chat_id"],
            "❌ <b>Upload failed.</b>\n"
            "Check that the bot is an administrator in your private channel "
            "and has permission to post messages.\n\n"
            "Please try Upload Now again after fixing the channel settings."
        )
    except Exception:
        logger.exception("Unexpected error while saving file")
        pending_uploads[user_id]["busy"] = False
        bot.send_message(
            pending["chat_id"],
            "❌ Something went wrong while saving the file. Please try again."
        )


@bot.message_handler(func=lambda message: True, content_types=["text"])
def text_fallback(message):
    if (message.text or "").startswith("/"):
        bot.reply_to(message, "Use /start to see how to upload and share files.")
    else:
        bot.reply_to(
            message,
            "📁 Please send a file or media message, then tap <b>⬆️ Upload Now</b>."
        )


def main():
    init_db()
    try:
        me = bot.get_me()
        logger.info("Bot started: @%s", me.username)
        logger.info("Database path: %s", DATABASE_PATH)
        logger.info("Private channel configured: %s", CHANNEL_ID)
        bot.infinity_polling(
            skip_pending=True,
            timeout=30,
            long_polling_timeout=30,
            allowed_updates=["message", "callback_query"]
        )
    except KeyboardInterrupt:
        logger.info("Bot stopped by keyboard interrupt")
    except Exception:
        logger.exception("Bot stopped due to an unexpected error")
        raise


if __name__ == "__main__":
    main()
