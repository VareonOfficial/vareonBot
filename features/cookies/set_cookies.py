import os
import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from main.state import sessions, awaiting_cookie
from main.config import logger, COOKIES_PATH
from pathlib import Path
from features.cookies.spotify_cookies import set_spotify_cookies, remove_spotify_cookies
from features.cookies.youtube_cookies import set_youtube_cookies, remove_youtube_cookies

async def cookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🍪 *Set cookies*\n\n"
        "Set cookies or remove them for your account.\n\n"
        "⚠️ *Security Tip*: Never share your session cookies with anyone else. " 
        "Our bot encrypts this data and uses it only for the requested tasks.\n\n"
        "Choose a service below:"
    )
    user_id = update.effective_user.id
    session_data = sessions.get(user_id)
    vareon_id = session_data.get("vareon_id")
    
    yt_cookie_path = COOKIES_PATH / "youtube" / f"{vareon_id}.txt"
    sp_cookie_path = COOKIES_PATH / "spotify" / f"{vareon_id}.txt"

    keyboard = []
    # --- YouTube button ---
    if os.path.exists(yt_cookie_path):
        keyboard.append([
            InlineKeyboardButton(
                "➖ Disconnect YouTube",
                callback_data="cookie_options|youtube_remove"
            )
        ])
    else:
        keyboard.append([
            InlineKeyboardButton(
                "➕ Connect YouTube",
                callback_data="cookie_options|youtube_add"
            )
        ])

    # --- Spotify button ---
    if os.path.exists(sp_cookie_path):
        keyboard.append([
            InlineKeyboardButton(
                "➖ Disconnect Spotify",
                callback_data="cookie_options|spotify_remove"
            )
        ])
    else:
        keyboard.append([
            InlineKeyboardButton(
                "➕ Connect Spotify",
                callback_data="cookie_options|spotify_add"
            )
        ])
    keyboard.append([
        InlineKeyboardButton(
            "❌ Close",
            callback_data="_common_menu:close:cookies",
            style="danger"
        )
    ])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    elif update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )

# ========================= CALLBACK ROUTER =========================
async def cookies_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    try:
        _, service = data.split("|", 1)
    except ValueError:
        return

    if service == "youtube_add":
        await set_youtube_cookies(update, context)

    elif service == "youtube_remove":
        await remove_youtube_cookies(update, context)

    elif service == "spotify_add":
        await set_spotify_cookies(update, context)

    elif service == "spotify_remove":
        await remove_spotify_cookies(update, context)

################################
# 📂 SAVE USER COOKIE FILE
################################
async def save_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    service = awaiting_cookie.get(user_id)
    if not service:
        return

    message = update.message

    # ===== TEXT SAFETY =====
    if message.text:
        if message.text.strip() == "/cancel":
            awaiting_cookie.pop(user_id, None)
            await message.reply_text("❌ Operation cancelled.")
            return

        if not message.text.startswith("/"):
            await message.reply_text(
                "⚠️ Please upload your exported cookies JSON file, not plain text."
            )
            return

    # ===== FILE VALIDATION =====
    if not message.document:
        return

    doc = message.document

    # ✅ Reject non-JSON mime types
    if doc.mime_type not in ("text/plain"):
        await message.reply_text("❌ Invalid file type. Please upload a NETSCAPE cookie file.")
        return

    # ✅ Reject wrong extensions
    if not doc.file_name.lower().endswith(".txt"):
        await message.reply_text("❌ File must be a .txt cookie export.")
        return

    session_data = sessions.get(user_id)
    if not session_data:
        return

    vareon_id = session_data.get("vareon_id")

    file = await doc.get_file()

    # 👉 TEMP FILE FIRST (important)
    temp_path = COOKIES_PATH / ".tmp" / f"{vareon_id}_cookie.txt"
    os.makedirs(temp_path.parent, exist_ok=True)
    await file.download_to_drive(temp_path)
    logger.info(f"[COOKIE] Download complete: {temp_path} | size={os.path.getsize(temp_path)} bytes")

    # ===== NETSCAPE VALIDATION + PARSE =====
    parsed_cookies = []
    try:
        with open(temp_path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        valid = False
        for line in lines:
            if line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 6:
                parts = line.split()
            if len(parts) >= 7:
                valid = True
                # Parse into dict: domain, flag, path, secure, expiry, name, value
                parsed_cookies.append({
                    "domain": parts[0],
                    "path":   parts[2],
                    "secure": parts[3],
                    "expiry": parts[4],
                    "name":   parts[5],
                    "value":  parts[6],
                })
        if not valid:
            raise ValueError("Not a valid Netscape cookie file")

    except Exception as e:
        os.remove(temp_path)
        await message.reply_text("❌ Invalid NETSCAPE file. Please upload a valid cookie export.")
        logger.error(f"[COOKIE] Invalid NETSCAPE uploaded: {e}")
        return

    # ===== FINAL SAVE =====
    final_path = COOKIES_PATH / service / f"{vareon_id}.txt"
    os.makedirs(os.path.dirname(final_path), exist_ok=True)
    os.replace(temp_path, final_path)
    logger.info(f"[COOKIE] Cookie stored successfully at: {final_path}")

    # ===== DELETE ORIGINAL MESSAGE =====
    try:
        await context.bot.delete_message(
            chat_id=message.chat_id,
            message_id=message.message_id
        )
    except Exception as e:
        logger.warning(f"Failed to delete cookie message: {e}")

    # ===== SERVICE-SPECIFIC EXPIRY =====
    expiry_msg = ""

    def get_expiry_str(cookie_name):
        cookie = next((c for c in parsed_cookies if c["name"] == cookie_name), None)
        if cookie and cookie["expiry"].replace(".", "", 1).isdigit():
            ts = int(float(cookie["expiry"]))
            return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%a, %d %b %Y")
        return None

    if service == "spotify":
        expiry_date = get_expiry_str("sp_dc")
        if expiry_date:
            expiry_msg = f"\nYour account session will expire on {expiry_date} (UTC)."

    elif service == "youtube":
        expiry_date = get_expiry_str("SID")
        if expiry_date:
            expiry_msg = f"\nYour account session will expire on {expiry_date} (UTC)."

    # ===== RESPONSE =====
    await message.reply_text(
        f"✅ {service.capitalize()} account connected successfully!{expiry_msg}"
    )
    awaiting_cookie.pop(user_id, None)