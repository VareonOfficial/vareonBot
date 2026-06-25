"""
files.py
────────
Handles Telegram file downloads via tdl.
Queue management is fully delegated to tdl_queue.py.
"""

import os
import asyncio
import time
import re
from telegram import Update, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackContext
from main.state import sessions, awaiting_id, awaiting_cookie, report_mode, broadcast_mode
from main.config import BASE_DIR, PRIVATE_GROUP_ID, logger, USERS_PATH, STORAGE_PATH
from main.utils import format_size
from features.files.tdl_queue import queue_tdl_task
from features.storage import get_folder_size, STORAGE_QUOTA_BYTES, get_status, build_bar, smart_format, STORAGE_QUOTA_LABEL
from vareon_analytics.vr_log import log_to_db, generate_task_id

user_locks = {}
   
async def files(update: Update, context: CallbackContext):
    """
    A simple greeting to let the user know the bot is standing by for files.
    """
    msg = update.message if update.message else update.callback_query.message

    await msg.reply_text(
        "✨ **VareonBot is Always-Ready**\n\n"
        "I'm standing by! You don't need to run any commands—just send or "
        "forward any file directly to this chat, and I'll immediately start "
        "the upload on your instructions. 🚀",
        parse_mode="Markdown"
    )

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user_id = update.effective_user.id

    if message.chat.type != "private":
        return

    if awaiting_cookie.get(user_id):
        return

    if awaiting_id.get(user_id):
        return
    if report_mode.get(user_id, False):
        return
    
    if broadcast_mode.get(user_id):
        return
    
    if user_id not in sessions:
        await message.reply_text("❌ You must login first.")
        return

    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()

    if user_locks[user_id].locked():
        await message.reply_text("⚠️ You already have a download running.")
        return

    if message.document:
        file_name = message.document.file_name
        file_size = message.document.file_size
        file_type = "document"
    elif message.video:
        file_name = message.video.file_name or f"video_{int(time.time())}.mp4"
        file_size = message.video.file_size
        file_type = "video"
    elif message.photo:
        file_name = f"photo_{int(time.time())}.jpg"
        file_size = message.photo[-1].file_size
        file_type = "photo"
    elif message.audio:
        file_name = message.audio.file_name or f"audio_{int(time.time())}.mp3"
        file_size = message.audio.file_size
        file_type = "audio"
    elif message.voice:
        file_name = f"voice_{int(time.time())}.ogg"
        file_size = message.voice.file_size
        file_type = "voice"
    elif message.video_note:
        file_name = f"video_note_{int(time.time())}.mp4"
        file_size = message.video_note.file_size
        file_type = "video_note"
    else:
        await message.reply_text("❌ Unsupported file.")
        return
    
    # ── Generate a fresh task_id for this entire download lifecycle ──────────
    task_id = generate_task_id()
    context.user_data["task_id"] = task_id
    context.user_data["download_start_time"] = None

    forwarded = await message.forward(chat_id=PRIVATE_GROUP_ID)
    chat_id = str(forwarded.chat.id)
    msg_id = forwarded.message_id
    internal_chat = chat_id[4:]
    public_link = f"https://t.me/c/{internal_chat}/{msg_id}"
    total_size = format_size(file_size)

    session_data = sessions[user_id]
    vareon_id = session_data["vareon_id"]
    log_to_db(
        vareon_id=vareon_id,
        tg_user_id=user_id,
        event_type="FILE_RECEIVED",
        function_name="handle_file",
        task_id=task_id,
        details={
            "file_name": file_name,
            "file_size": file_size,
            "file_type": file_type,
            "forwarded_link": public_link,
        },
        action_status={"status": "in_progress"},
    )

    context.user_data["pending_download"] = {
        "file_name": file_name,
        "file_size": file_size,
        "total_size": total_size,
        "file_type": file_type,
        "original_message_id": message.message_id,
        "forwarded_link": public_link,
        "progress_message": None,
    }

    base_dir = os.path.abspath(f"{USERS_PATH}/{vareon_id:08d}")
    os.makedirs(base_dir, exist_ok=True)

    if "path_stack" not in context.user_data:
        context.user_data["path_stack"] = [base_dir]
    else:
        context.user_data["path_stack"] = [base_dir]

    context.user_data["path_map"] = {}

    context.user_data["current_mode"] = "file_download_select"
    from main.dir_update import show_download_folder_menu
    await show_download_folder_menu(update, context)

async def run_tdl_download(progress_msg, url, path, file_name, context, user_id, total_size=None):
    """
    Public entry-point for downloads.
    Queues the task through the shared tdl lock so uploads and downloads
    never run concurrently against the same bolt DB.
    """
    # ── Log the path the user selected before queuing ─────────────────────────
    session_data = sessions.get(user_id, {})
    vareon_id = session_data.get("vareon_id")
    task_id = context.user_data.get("task_id")
    if vareon_id and task_id:
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            event_type="DOWNLOAD_PATH_SELECTED",
            function_name="run_tdl_download",
            task_id=task_id,
            details={
                "destination_path": path,
            },
            action_status={"status": "in_progress"},
        )

    async def _download_task(cancel_btn: InlineKeyboardMarkup):
        await _do_download(
            user_id=user_id,
            progress_msg=progress_msg,
            url=url,
            path=path,
            file_name=file_name,
            context=context,
            total_size=total_size,
            cancel_btn=cancel_btn,
        )

    await queue_tdl_task(
        progress_msg=progress_msg,
        context=context,
        user_id=user_id,
        file_name=file_name,
        kind="download",
        task_fn=_download_task,
    )


# ── Private: actual download logic (runs only when lock is held) ──────────────

async def _do_download(user_id: int, progress_msg, url, path, file_name, context, total_size, cancel_btn):
    cmd = [
        "tdl", "dl",
        "-u", url,
        "--storage", f"type=bolt,path={STORAGE_PATH}",
        "-d", path,
        "--threads", "16",
        "--pool", "0",
        "--limit", "8",
        "--continue",
    ]

    logger.info(f"Starting download: {file_name}")
    logger.info(f"Command: {' '.join(cmd)}")

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    context.user_data["active_process"] = process
    context.user_data["active_download_path"] = path

    percent = 0.0
    downloaded = "0B"
    speed = "0B/s"
    eta = "N/A"
    last_update = 0
    download_started = False

    try:
        while True:
            if process.returncode is not None:
                break

            if context.user_data.get("tdl_cancelled"):
                process.kill()
                break

            try:
                line_bytes = await asyncio.wait_for(process.stdout.readline(), timeout=0.3)
            except asyncio.TimeoutError:
                continue

            if not line_bytes:
                await asyncio.sleep(0.05)
                continue

            line_raw = line_bytes.decode("utf-8", errors="ignore").strip()
            line = re.sub(r'\x1b\[[0-9;]*[mK]', '', line_raw)

            if (
                not line
                or "%" not in line
                or "CPU:" in line
                or "DEBUG:" in line
            ):
                continue

            download_started = True
            # ── Capture wall-clock start time on first real progress line ────
            if context.user_data.get("download_start_time") is None:
                context.user_data["download_start_time"] = time.time()
            percent_match = re.search(r'(\d+(?:\.\d+)?)%', line)
            downloaded_match = re.search(
                r'(\d+(?:\.\d+)?\s*[KMGT]?i?B)\s+in\s',
                line
            )
            speed_matches = re.findall(r'(\d+(?:\.\d+)?\s*[KMGT]?i?B/s)', line)
            speed_match_val = speed_matches[-1].strip() if speed_matches else None
            eta_match = re.search(r'~?ETA:\s*([^;^\]]+)', line)

            if percent_match:
                new_percent = float(percent_match.group(1))
                if new_percent >= percent:
                    percent = new_percent

            if downloaded_match:
                downloaded = downloaded_match.group(1).strip()

            if speed_match_val:
                speed = speed_match_val

            if eta_match:
                eta = eta_match.group(1).strip()
            now = time.time()
            if percent > 0 and now - last_update >= 1.8:
                try:
                    bar = "▪️" * int(percent // 10) + "▫️" * (10 - int(percent // 10))
                    size_text = f"{downloaded} of {total_size}" if total_size else downloaded
                    await progress_msg.edit_text(
                        f"⬇️ *Downloading*\n"
                        f"`{file_name}`\n\n"
                        f"*Progress:* {percent:.1f}%\n"
                        f"{bar}\n"
                        f"*{size_text}*\n"
                        f"🚀 *Speed:* {speed}\n"
                        f"⏳ *ETA:* {eta}",
                        parse_mode="Markdown",
                        reply_markup=cancel_btn,
                    )
                    last_update = now
                except Exception as e:
                    logger.warning(f"Failed to edit progress message: {e}")

    except asyncio.CancelledError:
        logger.info("Download task cancelled")
    finally:
        if process.returncode is None:
            try:
                process.kill()
                await process.wait()
                logger.info("Process killed")
            except Exception:
                pass
        await process.wait()
        returncode = process.returncode
        logger.info(f"tdl download exited with code: {returncode}")

        # ── Rename the downloaded file from tdl's default name to file_name ──
        final_name = file_name
        try:
            parts = url.split("/")
            chat_id = parts[-2]
            msg_id = parts[-1]
            prefix = f"{chat_id}_{msg_id}_"

            for _ in range(15):
                for f in os.listdir(path):
                    if f.startswith(prefix) and not f.endswith((".tmp", ".part")):
                        old_path = os.path.join(path, f)
                        new_path = os.path.join(path, file_name)
                        if os.path.exists(new_path):
                            name, ext = os.path.splitext(file_name)
                            new_path = os.path.join(path, f"{name}_fixed{ext}")
                        os.rename(old_path, new_path)
                        final_name = os.path.basename(new_path)
                        logger.info(f"Renamed {f} → {final_name}")
                        break
                else:
                    await asyncio.sleep(0.3)
                    continue
                break
        except Exception as e:
            logger.error(f"Rename error: {e}")

        # ── Compute time taken (seconds) ──────────────────────────────────────
        download_end_time = time.time()
        download_start_time = context.user_data.get("download_start_time")
        time_taken_seconds = (
            round(download_end_time - download_start_time)
            if download_start_time else None
        )

        # ── Read actual file size from disk after rename ───────────────────────
        actual_file_size = None
        try:
            final_path = os.path.join(path, final_name)
            if os.path.exists(final_path):
                actual_file_size = os.path.getsize(final_path)
        except Exception as e:
            logger.warning(f"Could not stat final file for analytics: {e}")

        # ── Resolve vareon_id / task_id for logging ───────────────────────────
        session_data = sessions[user_id]
        vareon_id = session_data["vareon_id"]
        task_id = context.user_data.get("task_id")

        # ── Final status message ───────────────────────────────────────────────
        if returncode == 0 and download_started:
            base_dir = os.path.abspath(f"{USERS_PATH}/{vareon_id:08d}")
            used_bytes = get_folder_size(base_dir)
            free_bytes = max(0, STORAGE_QUOTA_BYTES - used_bytes)
            pct        = min(100.0, (used_bytes / STORAGE_QUOTA_BYTES) * 100)
            bar        = build_bar(pct)
            status     = get_status(pct)
            await progress_msg.edit_text(
                f"✅ <b>Download complete!</b>\n\n"
                f"📁 <b>Filename:</b> <code>{final_name}</code>\n"
                f"📍 <b>Location:</b> <code>{path}</code>\n\n"
                f"<b>Storage Insight:-</b>\n"
                f"<blockquote>"
                f"{bar}  <b>{pct:.2f}%</b>\n"
                f"├ Used  →  <b>{smart_format(used_bytes)}</b> of {STORAGE_QUOTA_LABEL}\n"
                f"├ Free  →  <b>{smart_format(free_bytes)}</b>\n"
                f"└ State →  {status}"
                f"</blockquote>\n"
                f"💡 Use /storage to view complete storage details\n",
                parse_mode="HTML",
            )
            logger.info(f"✅ Download completed: {final_name}")
            if task_id:
                log_to_db(
                    vareon_id=vareon_id,
                    tg_user_id=user_id,
                    event_type="DOWNLOAD_COMPLETE",
                    function_name="_do_download",
                    task_id=task_id,
                    details={
                        "time_taken": time_taken_seconds,
                    },
                    action_status={"status": "success"},
                )
        else:
            error_msg = (
                "❌ Download did not start (check URL or tdl installation)"
                if not download_started else "❌ Download failed or was cancelled"
            )
            await progress_msg.edit_text(
                f"{error_msg}\n\n"
                f"📄 File: `{file_name}`\n"
                f"🔢 Exit code: {returncode}",
                parse_mode="Markdown",
            )
            logger.error(f"Download failed | Exit: {returncode} | Started: {download_started}")
            if task_id:
                log_to_db(
                    vareon_id=vareon_id,
                    tg_user_id=user_id,
                    event_type="DOWNLOAD_FAILED",
                    function_name="_do_download",
                    task_id=task_id,
                    details={
                        "time_taken": time_taken_seconds,
                        "exit_code": returncode,
                        "download_started": download_started,
                    },
                    action_status={"status": "failed"},
                )
            
        context.user_data.pop("active_process", None)
        context.user_data.pop("active_download_path", None)
        context.user_data.pop("pending_download", None)
        context.user_data.pop("tdl_cancelled", None)