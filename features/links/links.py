import os
import sqlite3

from tcod import context
from main.config import VAREON_DB
import uuid
import re
import time
import asyncio
import aiohttp
import mimetypes
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackContext
from main.dir_update import set_download_location, show_download_folder_menu
from main.utils import (
    sanitize_callback_data,
    format_size,
)
from main.state import download_tasks, sessions, report_mode, download_tasks
from main.config import USERS_PATH, logger
from features.links.direct_link_progress import download_file_with_progress
from features.links.yt_shorts import handle_shorts_download, show_shorts_upload_prompt
from features.shared.yt_dlp import show_youtube_quality_menu
from vareon_analytics.vr_log import log_to_db, generate_task_id
################################
# Link Download/Upload Handlers
################################

URL_PATTERN = re.compile(r"^https?://.*")
YOUTUBE_PATTERN = re.compile(
    r'(https?://)?(www\.)?(youtube\.com|youtu\.be|youtube\.com/shorts|youtube\.com/live|youtube\.com/playlist)/',
    re.IGNORECASE
)

# @log_wrapper("/link")
async def link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip()
    
    logger.info(f"[LINK_HANDLER] User {user_id} triggered link command")
    
    if user_id not in sessions:
        logger.warning(f"[LINK_HANDLER] User {user_id} not logged in")
        await update.message.reply_text("❌ Please login first using /login.")
        return
    if report_mode.get(user_id, False):
        logger.info(f"[LINK_HANDLER] User {user_id} in report mode, ignoring")
        return

    if text == "/link":
        logger.info(f"[LINK_HANDLER] User {user_id} requested help text")
        await update.message.reply_text(
            "📌 To use this feature, send a valid link after the command.\n\n"
            "✅ Example:\n"
            "`/link https://example.com/file.zip`\n\n"
            "This will start the link processing and present download options. "
            "Please provide a proper link to continue.",
            parse_mode="Markdown"
        )
        return

    if not text.startswith("/link "):
        return

    url = text[6:].strip()
    logger.info(f"[LINK_HANDLER] URL received: {url}")
    
    if not URL_PATTERN.match(url):
        logger.warning(f"[LINK_HANDLER] Invalid URL from user {user_id}: {url}")
        await update.message.reply_text("❌ Invalid URL. Please provide a valid link.")
        return
    
    context.user_data["link_url"] = url

    # Detect YouTube and set mode
    if YOUTUBE_PATTERN.search(url):
        context.user_data["youtube_mode"] = True
        logger.info(f"[LINK_HANDLER] YouTube URL detected: {url}")
    else:
        context.user_data["youtube_mode"] = False
        logger.info(f"[LINK_HANDLER] Regular URL detected: {url}")

    # ====================== STAGE 1: LINK_RECEIVED LOG ======================
    task_id = generate_task_id()
    context.user_data["task_id"] = task_id
    context.user_data["download_start_time"] = None

    vareon_id = sessions.get(user_id, {}).get("vareon_id", "unknown")
    log_to_db(
        vareon_id=vareon_id,
        tg_user_id=user_id,
        event_type="LINK_RECEIVED",
        function_name="link_handler",
        task_id=task_id,
        details={
            "url": url,
            "link_type": "youtube" if context.user_data.get("youtube_mode") else "direct"
        },
        action_status={"status": "in_progress"}
    )
    logger.info(f"[LINK_HANDLER] LINK_RECEIVED logged | task_id={task_id}")
    # =========================================================================

    # Continue to your existing link processing
    await handle_link(update, context)
            
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    logger.info(f"[HANDLE_LINK] Processing link for user {user_id}")
    user_data = sessions.get(user_id)
    
    if not user_data:
        logger.warning(f"[HANDLE_LINK] No session data for user {user_id}")
        await update.message.reply_text("❌ Please login first using /login.")
        return

    url = context.user_data.get("link_url")
    if not url:
        url = update.message.text.strip()[6:].strip()
    logger.info(f"[HANDLE_LINK] URL to process: {url}")
    
    # ====================== DEFAULT DOWNLOAD PATH HANDLING ======================
    with sqlite3.connect(VAREON_DB) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT default_download_enabled, default_download_path
            FROM user_settings
            WHERE telegram_user_id = ?
        """, (user_id,))
        row = cursor.fetchone()
    if row:
        enabled, default_path = row
        if enabled and default_path and os.path.exists(default_path):
            logger.info(f"[HANDLE_LINK] Using default path for user {user_id}: {default_path}")
            context.user_data["selected_download_path"] = default_path

            if context.user_data.get("youtube_mode"):
                asyncio.create_task(show_youtube_quality_menu(update, context))
            else:
                try:
                    await fetch_file_info(url, context)
                    await show_start_download_button(update, context)
                    # ====================== STAGE 2: PATH_SELECTED LOG (default path) ======================
                    file_info = context.user_data.get("file_info", {})
                    task_id = context.user_data.get("task_id")
                    vareon_id = sessions.get(user_id, {}).get("vareon_id", "unknown")
                    log_to_db(
                        vareon_id=vareon_id,
                        tg_user_id=user_id,
                        event_type="PATH_SELECTED",
                        function_name="handle_link",
                        task_id=task_id,
                        details={
                            "path": default_path,
                            "file_name": file_info.get("raw_name"),
                            "size_bytes": file_info.get("size"),
                            "resume_supported": context.user_data.get("resume_supported")
                        },
                        action_status={"status": "in_progress"}
                    )
                    logger.info(f"[HANDLE_LINK] PATH_SELECTED logged | task_id={task_id}")
                except Exception as e:
                    logger.error(f"[HANDLE_LINK] Failed to fetch file info: {e}")
                    await update.message.reply_text(f"❌ Failed to fetch file info: {e}")
                    return
            return

    # ====================== SHOW FOLDER MENU (for both normal + YouTube) ======================
    user_folder = user_data.get('vareon_id')
    base_path = f"{USERS_PATH}/{user_folder}"
    logger.info(f"[HANDLE_LINK] Base path for user {user_id}: {base_path}")

    context.user_data["current_mode"] = "link_download_select"
    context.user_data["path_stack"] = [base_path]
    context.user_data["selected_download_path"] = None
    logger.info(f"[HANDLE_LINK] Showing folder menu for user {user_id}")

    if not context.user_data.get("youtube_mode"):
        try:
            await fetch_file_info(url, context)
        except Exception as e:
            logger.error(f"[HANDLE_LINK] Failed to fetch file info: {e}")
            await update.message.reply_text(f"❌ Failed to fetch file info:\n{e}")
            return
    msg1 = await show_download_folder_menu(update, context)  
    msg2 = await show_shorts_upload_prompt(update, context)
    context.user_data["folder_menu_msg_id"] = msg1.message_id
    context.user_data["shorts_prompt_msg_id"] = msg2.message_id
    
def file_exists_in_dir(download_path: str, base_name: str) -> str | None:
    """
    Returns matched filename if exists, else None
    """
    logger.debug(f"[FILE_EXISTS] Checking if '{base_name}' exists in {download_path}")
    try:
        files = os.listdir(download_path)

        for f in files:
            name_no_ext = os.path.splitext(f)[0].strip().lower()

            if name_no_ext == base_name:
                return f

    except Exception as e:
        logger.error(f"[FILE_EXISTS] Error checking file: {e}")
        return None

    return None

async def show_start_download_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    only for default path
    """
    logger.info(f"[SHOW_DOWNLOAD_BUTTON] Preparing download button for user {update.effective_chat.id}")

    file_info = context.user_data.get("file_info", {})

    raw_name = file_info.get("raw_name", "Unknown File")
    size = file_info.get("size", 0)

    resume_supported = context.user_data.get("resume_supported", False)
    resume_text = "✅ Resume supported" if resume_supported else "❌ Resume not supported"

    download_path = context.user_data.get("selected_download_path", "")

    base_name = os.path.splitext(raw_name)[0].strip().lower()

    matched_file = None
    if download_path and base_name:
        matched_file = file_exists_in_dir(download_path, base_name)
        logger.debug(f"[SHOW_DOWNLOAD_BUTTON] File match result: {matched_file}")

    if matched_file:
        logger.info(f"[SHOW_DOWNLOAD_BUTTON] File already exists: {matched_file}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"✅ File already exists!\n\n`{matched_file}`",
            parse_mode="Markdown",
        )
        return

    keyboard = [
        [InlineKeyboardButton("📤 Start Download", callback_data="start_download", style="primary")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_process", style="danger")]
    ]

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"📄 Filename: `{raw_name}`\n\n"
            f"📦 Size: `{format_size(size)}`\n"
            f"{resume_text}\n"
            f"📂 Using your *default download location*\n"
            f"📁 Path: `{download_path}`"
        ),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    logger.info(f"[SHOW_DOWNLOAD_BUTTON] Download button sent for file: {raw_name}")

async def download_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    after user clicks on the menu options and chooses a folder he comes back here, to start download
    or cancel it.
    
    (NOT FOR DEFUALT)
    """
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    
    data = query.data
    logger.info(f"[DOWNLOAD_BUTTON_HANDLER] Callback query from user {user_id}: {data}")
    is_youtube = context.user_data.get("youtube_mode", False)

    # Prevent double start
    if context.user_data.get("current_download_id") and data == "start_download":
        logger.warning(f"[DOWNLOAD_BUTTON_HANDLER] Duplicate start attempt from user {user_id}")
        await query.answer("⚠️ Download already in progress", show_alert=True)
        return
    if data == "download_here_tg":
        logger.info(f"[DOWNLOAD_BUTTON_HANDLER] User {user_id} selected download here (Telegram)")
        if context.user_data.get("shorts_url"):
            logger.info(f"[DOWNLOAD_BUTTON_HANDLER] Shorts URL detected for user {user_id}")
            await handle_shorts_download(update=update, context=context)
            return
        
    if data == "download_here_local":
        logger.info(f"[DOWNLOAD_BUTTON_HANDLER] User {user_id} selected download here")
        await set_download_location(update, context)

        file_info = context.user_data.get("file_info", {})
        download_path = context.user_data.get("selected_download_path", "")

        expected_size = file_info.get("size", 0)
        size_text = format_size(expected_size)

        raw_name = file_info.get("raw_name", "")
        base_name = os.path.splitext(raw_name)[0].strip().lower()
        matched_file = None

        if download_path and base_name:
            matched_file = file_exists_in_dir(download_path, base_name)

        if matched_file:
            logger.info(f"[DOWNLOAD_BUTTON_HANDLER] File already exists: {matched_file}")
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"✅ File already exists!\n\n`{matched_file}`",
                parse_mode="Markdown",
            )
            context.user_data.pop("file_info", None)
            return
        
        if is_youtube:
            logger.info(f"[DOWNLOAD_BUTTON_HANDLER] YouTube mode triggered for user {user_id}")
            asyncio.create_task(show_youtube_quality_menu(update, context))
            return
        else:
            # ====================== STAGE 2: PATH_SELECTED LOG (manual path) ======================
            task_id = context.user_data.get("task_id")
            vareon_id = sessions.get(user_id, {}).get("vareon_id", "unknown")
            log_to_db(
                vareon_id=vareon_id,
                tg_user_id=user_id,
                event_type="PATH_SELECTED",
                function_name="download_button_handler",
                task_id=task_id,
                details={
                    "path": download_path,
                    "file_name": file_info.get("raw_name"),
                    "size_bytes": file_info.get("size"),
                    "resume_supported": context.user_data.get("resume_supported")
                },
                action_status={"status": "in_progress"}
            )
            logger.info(f"[DOWNLOAD_BUTTON_HANDLER] PATH_SELECTED logged | task_id={task_id}")
            # ======================================================================================

        resume_supported = context.user_data.get("resume_supported", False)
        resume_text = "✅ Resume supported" if resume_supported else "❌ Resume not supported"

        prev_id = context.user_data.get("last_option_msg_id")
        if prev_id:
            try:
                await context.bot.delete_message(query.message.chat_id, prev_id)
            except Exception as e:
                logger.warning("[MENU] delete failed: %s", e)

        msg = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                f"📄 Filename: `{raw_name}`\n"
                f"📦 Size: `{size_text}`\n"
                f"{resume_text}\n"
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Start Download", callback_data="start_download", style="primary")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_process", style="danger")]
            ])
        )

        context.user_data["last_option_msg_id"] = msg.message_id
        logger.info(f"[DOWNLOAD_BUTTON_HANDLER] Download options sent for file: {raw_name}")
        return
    
    if data == "start_download":
        logger.info(f"[DOWNLOAD_BUTTON_HANDLER] User {user_id} starting download")
        file_info = context.user_data.get("file_info")

        if not file_info:
            logger.error(f"[DOWNLOAD_BUTTON_HANDLER] No file info for user {user_id}")
            await query.edit_message_text("❌ Session expired. Please send the link again.")
            return

        raw_name = file_info.get("raw_name", "file")
        logger.info(f"[DOWNLOAD_BUTTON_HANDLER] Starting download for: {raw_name}")

        # SAFE ID
        download_id = f"{user_id}_{uuid.uuid4().hex}"
        context.user_data["current_download_id"] = download_id
        logger.debug(f"[DOWNLOAD_BUTTON_HANDLER] Download ID generated: {download_id}")

        # ====================== STAGE 3: DOWNLOAD_STARTED LOG ======================
        context.user_data["download_start_time"] = time.time()
        task_id = context.user_data.get("task_id")
        vareon_id = sessions.get(user_id, {}).get("vareon_id", "unknown")
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            function_name="download_button_handler",
            event_type="DOWNLOAD_STARTED",
            task_id=task_id,
            details={"download_id": download_id},
            action_status={"status": "in_progress"}
        )
        logger.info(f"[DOWNLOAD_BUTTON_HANDLER] DOWNLOAD_STARTED logged | task_id={task_id}")
        # ============================================================================

        msg = await query.edit_message_text(
            f"⬇️ Downloading `{raw_name}`...",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([])
        )

        task = asyncio.create_task(
            download_file_with_progress(
                context, query, msg.message_id, file_info, download_id
            )
        )

        download_tasks[download_id] = task
        logger.info(f"[DOWNLOAD_BUTTON_HANDLER] Download task created for {raw_name}")
    
async def fetch_file_info(url, context):
    logger.info(f"[FETCH_FILE_INFO] Fetching file info for: {url}")
    timeout = aiohttp.ClientTimeout(total=30)

    if not url or not url.startswith(("http://", "https://")):
        logger.error(f"[FETCH_FILE_INFO] Invalid URL format: {url}")
        raise Exception("Invalid URL format")

    async with aiohttp.ClientSession(timeout=timeout) as session:
        response = None

        # ====================== REQUEST PHASE ======================
        for attempt in range(2):
            logger.info(f"[FETCH] Attempt {attempt+1} (HEAD)")

            try:
                response = await session.head(url, allow_redirects=True)
                if response.status < 400:
                    break
            except Exception as e:
                logger.warning(f"[FETCH] HEAD failed: {e}")

            logger.info(f"[FETCH] Attempt {attempt+1} (GET fallback)")
            try:
                response = await session.get(url, allow_redirects=True)
                if response.status < 400:
                    break
            except Exception as e:
                logger.error(f"[FETCH] GET failed: {e}")
                if attempt == 1:
                    raise Exception(f"Failed to connect: {e}")

        if not response:
            logger.error("[FETCH] No response received")
            raise Exception("No response from server")

        if response.status >= 400:
            logger.error(f"[FETCH] HTTP error status={response.status}")
            raise Exception(f"HTTP error: {response.status}")

        headers = response.headers
        logger.info(f"[FETCH] Headers received: {dict(headers)}")

        # ====================== METADATA ======================
        # Size
        size_raw = headers.get("Content-Length")
        size = int(size_raw) if size_raw and size_raw.isdigit() else 0

        # Content-Type
        ct = headers.get("Content-Type", "").split(";")[0].strip()
        content_type = ct or "application/octet-stream"

        logger.info(f"[FETCH] Parsed content_type={content_type} size={size}")

        # Reject HTML
        if "text/html" in content_type.lower():
            logger.error("[FETCH] HTML detected instead of file")
            raise Exception("URL does not point to a downloadable file")

        # ====================== FILENAME ======================
        cd = headers.get("Content-Disposition", "")
        name = None

        if "filename=" in cd:
            name = cd.split("filename=")[-1].strip('\"\' ')
            logger.info(f"[FETCH] Filename from Content-Disposition: {name}")

        if not name:
            parsed = url.split("/")[-1].split("?")[0]
            if parsed and "." in parsed:
                name = parsed
                logger.info(f"[FETCH] Filename from URL: {name}")

        if not name:
            ext = mimetypes.guess_extension(ct) or ".bin"
            name = f"file_{int(time.time())}{ext}"
            logger.warning(f"[FETCH] Fallback filename generated: {name}")

        name = name.strip().replace("\n", "").replace("\r", "")

        # ====================== RESUME SUPPORT ======================
        resume_supported = False

        if headers.get("Accept-Ranges", "").lower() == "bytes":
            resume_supported = True
            logger.info("[FETCH] Resume supported via header")
        else:
            try:
                async with session.get(url, headers={"Range": "bytes=0-10"}) as r2:
                    if r2.status == 206:
                        resume_supported = True
                        logger.info("[FETCH] Resume supported via range test")
            except Exception as e:
                logger.warning(f"[FETCH] Resume check failed: {e}")

        # ====================== STORE STATE ======================
        file_info = {
            "url": url,
            "final_url": str(response.url),
            "name": sanitize_callback_data(name),
            "raw_name": name,
            "size": size,
            "content_type": content_type
        }

        context.user_data["resume_supported"] = resume_supported
        context.user_data["file_info"] = file_info

        logger.info(f"[FETCH] FINAL STORED file_info={file_info}")