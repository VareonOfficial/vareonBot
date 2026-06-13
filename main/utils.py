import re, asyncio
import os
import time
import math, json
import uuid
import subprocess
from datetime import timedelta
from typing import Callable
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (CallbackContext, ContextTypes)
from telegram.error import RetryAfter, TimedOut
from functools import wraps
from main.config import (RATE_LIMIT_PER_MINUTE, RATE_LIMIT_INTERVAL, logger, USERS_PATH)
from main.state import awaiting_id, awaiting_cookie, sessions, user_last_interaction

def get_user_session(user_id):
    return sessions.get(user_id)

def sanitize_callback_data(data):
    return data.replace('"', "").replace("'", "").replace("\n", "").replace(".", "_").strip()

def format_size(bytes):
    """Convert bytes to human-readable format"""
    if bytes == 0:
        return "0B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = int(math.floor(math.log(bytes, 1024)))
    p = math.pow(1024, i)
    s = round(bytes / p, 2)
    return f"{s}{units[i]}"

async def getid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    awaiting_id[user_id] = True
    
    await update.message.reply_text(
        "📥 Send me a video, document, or photo now and I’ll show you its file_id."
    )
async def cache_file_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not update.effective_user:
        return
    user_id = update.effective_user.id

    if not awaiting_id.get(user_id):
        return

    if awaiting_cookie.get(user_id):
        return

    if message.video:
        file_id = message.video.file_id
        await message.reply_text(f"🎥 Video file_id:\n<code>{file_id}</code>", parse_mode="HTML")

    elif message.document:
        file_id = message.document.file_id
        await message.reply_text(f"📄 Document file_id:\n<code>{file_id}</code>", parse_mode="HTML")

    elif message.photo:
        file_id = message.photo[-1].file_id
        await message.reply_text(f"🖼️ Photo file_id:\n<code>{file_id}</code>", parse_mode="HTML")

    else:
        await message.reply_text("⚠️ Please send a video, document, or photo.")

    awaiting_id.pop(user_id, None)
    
def ensure_folder_permissions(folder_path: str) -> str:
    """
    Ensures the folder exists and is writable.
    Returns a usable folder path (fallbacks if needed).
    """

    try:
        # Step 1: Create folder if it doesn't exist
        os.makedirs(folder_path, exist_ok=True)

        # Step 2: Check write permission
        if os.access(folder_path, os.W_OK):
            return folder_path

        logger.warning(f"No write access to: {folder_path}")

    except Exception as e:
        logger.warning(f"Primary path failed: {folder_path} | {e}")

    # 🚑 Fallback (guaranteed writable location)
    fallback = os.path.join(os.getcwd(), "downloads")
    try:
        os.makedirs(fallback, exist_ok=True)
        logger.info(f"Using fallback path: {fallback}")
        return fallback
    except Exception as e:
        logger.error(f"Fallback also failed: {e}")
        raise RuntimeError("No writable directory available")

def register_path(context, path):
    if "path_map" not in context.user_data:
        context.user_data["path_map"] = {}

    path_map = context.user_data["path_map"]

    # If path already registered, return existing UID
    for uid, stored_path in path_map.items():
        if stored_path == path:
            return uid

    # Otherwise create new UID
    uid = str(uuid.uuid4())
    path_map[uid] = path
    context.user_data["path_map"] = path_map

    return uid

def list_directory_contents(path, context):
    items = []

    try:
        for entry in os.scandir(path):

            name = entry.name
            if name.startswith("."):
                continue

            full_path = os.path.join(path, name)

            uid = register_path(context, full_path)

            if not uid:
                logger.warning("UID registration failed for %s", full_path)
                continue

            is_folder = entry.is_dir()

            items.append((uid, name, is_folder))

        items.sort(key=lambda x: (not x[2], x[1].lower()))

        logger.debug("Raw listing: %s | total=%d", path, len(items))

    except Exception as e:
        logger.error("Error reading folder %s: %s", path, e)

    return items

def format_speed(bytes_per_sec):
    """Convert bytes per second to human-readable format"""
    return format_size(bytes_per_sec) + "/s"

def format_time(seconds):
    """Convert seconds to human-readable time format"""
    return str(timedelta(seconds=int(seconds)))

        
async def edit_message_if_changed(context, chat_id, message_id, text, reply_markup=None, parse_mode=None):
    try:
        stored_messages = context.user_data.get("stored_messages", {})
        stored_key = f"{chat_id}_{message_id}"
        stored_data = stored_messages.get(stored_key, {"text": "", "markup": None})
        stored_text = stored_data["text"]
        stored_markup = stored_data["markup"]

        new_text_clean = re.sub(r'[*_`]', '', text) if parse_mode == "Markdown" else text
        stored_text_clean = re.sub(r'[*_`]', '', stored_text)
        new_markup_dict = reply_markup.to_dict() if reply_markup else None

        if new_text_clean == stored_text_clean and new_markup_dict == stored_markup:
            logger.debug(f"No change in message {message_id}. Skipping edit.")
            return

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
        logger.debug(f"Edited message {message_id} with new content.")

        stored_messages[stored_key] = {
            "text": text,
            "markup": new_markup_dict
        }
        context.user_data["stored_messages"] = stored_messages
    except Exception as e:
        logger.warning(f"Failed to edit message {message_id}: {e}")
        new_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
        context.user_data["last_menu_message_id"] = new_msg.message_id
        context.user_data.setdefault("stored_messages", {})[f"{chat_id}_{new_msg.message_id}"] = {
            "text": text,
            "markup": reply_markup.to_dict() if reply_markup else None
        }


def is_safe_to_delete(path, vareon_id):
    """Ensure path is within {USERS_PATH}/{vareon_id} and not the root folder."""
    root_dir = os.path.abspath(f"{USERS_PATH}/{vareon_id}")
    target_path = os.path.abspath(path)

    # Block root or anything above it
    if target_path == root_dir:
        return False

    # Block anything outside the user's folder
    if not target_path.startswith(root_dir + os.sep):
        return False

    return True


def rate_limit_interaction(func: Callable) -> Callable:
    """Decorator to throttle user interactions and manage API rate limits."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.callback_query.from_user.id if update.callback_query else update.message.from_user.id
        current_time = time.time()

        # Debounce rapid clicks
        last_time = user_last_interaction.get(user_id, 0)
        if current_time - last_time < RATE_LIMIT_INTERVAL:
            logger.debug(f"Debouncing interaction for user {user_id}")
            if update.callback_query:
                await update.callback_query.answer("Please wait a moment before clicking again.", show_alert=True)
            return

        user_last_interaction[user_id] = current_time

        # Track rate limit budget
        context.user_data.setdefault("rate_limit_count", []).append(current_time)
        context.user_data["rate_limit_count"] = [t for t in context.user_data["rate_limit_count"] if current_time - t < 60]
        if len(context.user_data["rate_limit_count"]) > RATE_LIMIT_PER_MINUTE:
            logger.warning(f"Rate limit budget exceeded for user {user_id}")
            if update.callback_query:
                await update.callback_query.answer("Too many actions. Please wait a minute.", show_alert=True)
            return

        try:
            return await func(update, context, *args, **kwargs)
        except RetryAfter as e:
            logger.error(f"Rate limit hit: Retry after {e.retry_after} seconds")
            if update.callback_query:
                await update.callback_query.answer(f"Rate limit exceeded. Please wait {e.retry_after} seconds.", show_alert=True)
            await asyncio.sleep(e.retry_after)
            return await func(update, context, *args, **kwargs)
        except TimedOut:
            logger.error("Request timed out")
            if update.callback_query:
                await update.callback_query.answer("Request timed out. Please try again.", show_alert=True)
            await asyncio.sleep(5)
            return await func(update, context, *args, **kwargs)

    return wrapper