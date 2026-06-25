from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (CallbackContext, ContextTypes)
from main.utils import format_size, format_speed, format_time, ensure_folder_permissions
import time
import os
import asyncio
import traceback
import httpx
import aiofiles
from telegram.ext import ContextTypes, CallbackContext
from main.state import download_status, download_tasks, sessions, download_tasks
from main.config import USERS_PATH, logger
from vareon_analytics.vr_log import log_to_db
from features.storage import get_folder_size, STORAGE_QUOTA_BYTES, get_status, build_bar, smart_format, STORAGE_QUOTA_LABEL

async def download_file_with_progress(context, query, message_id, file_info, download_id):
    user_id = query.from_user.id
    url = file_info.get("url", "")
    filename = file_info.get("raw_name", "").strip()
    total = file_info.get("size", 0)
    start_time = time.time()
    user_data = sessions.get(user_id)
    user_folder = user_data.get("vareon_id")
    base_dir = f"{USERS_PATH}/{user_folder}"
    download_path = file_info.get("download_path", base_dir)
    os.makedirs(download_path, exist_ok=True)
    download_path = ensure_folder_permissions(download_path)
    path = os.path.join(download_path, filename)

    initial_downloaded = os.path.getsize(path) if os.path.exists(path) else 0
    downloaded = initial_downloaded
    newly_downloaded = 0
    CHUNK_SIZE = 64 * 1024
    UPDATE_INTERVAL = 5
    last_update = start_time
    resume_supported = context.user_data.get("resume_supported", False)
    speed_samples = []

    if download_id not in download_status:
        download_status[download_id] = {"active": True, "paused": False, "downloaded": downloaded}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=60.0)) as client:
            while True:
                headers = {"Range": f"bytes={downloaded}-"} if downloaded > 0 else {}
                paused_during_stream = False

                async with client.stream("GET", url, headers=headers, follow_redirects=True) as response:
                    response.raise_for_status()
                    async with aiofiles.open(path, 'ab' if downloaded > 0 else 'wb') as f:
                        async for chunk in response.aiter_bytes(chunk_size=CHUNK_SIZE):
                            await asyncio.sleep(0)

                            if download_status.get(download_id, {}).get("paused", False):
                                # Break the stream immediately — don't write or buffer this chunk
                                paused_during_stream = True
                                break

                            await f.write(chunk)
                            downloaded += len(chunk)
                            newly_downloaded += len(chunk)
                            if download_id in download_status:
                                download_status[download_id]["downloaded"] = downloaded

                            current_time = time.time()
                            if current_time - last_update >= UPDATE_INTERVAL or downloaded == total:
                                elapsed = current_time - start_time + 1e-6
                                speed = newly_downloaded / elapsed
                                speed_samples.append(speed)
                                if len(speed_samples) > 5:
                                    speed_samples.pop(0)
                                avg_speed = sum(speed_samples) / len(speed_samples) if speed_samples else speed

                                try:
                                    await update_download_progress(
                                        update=query, context=context, message_id=message_id,
                                        downloaded=downloaded, total=total, speed=avg_speed,
                                        start_time=start_time, filename=filename, download_id=download_id
                                    )
                                except Exception as _e:
                                    if "not modified" not in str(_e).lower():
                                        raise

                                percent = (downloaded / total * 100) if total > 0 else 0
                                eta = (total - downloaded) / avg_speed if avg_speed > 0 else 0
                                logger.info(
                                    f"[{filename}] {percent:.1f}% | "
                                    f"{format_size(downloaded)} of {format_size(total)} | "
                                    f"Speed: {format_speed(avg_speed)} | "
                                    f"ETA: {format_time(eta)}"
                                )
                                last_update = current_time

                if paused_during_stream:
                    # Show paused message, then wait for resume
                    try:
                        await update_download_progress(
                            update=query, context=context, message_id=message_id,
                            downloaded=downloaded, total=total, speed=0.0,
                            start_time=start_time, filename=filename, download_id=download_id
                        )
                    except Exception as _e:
                        if "not modified" not in str(_e).lower():
                            raise
                    while download_status.get(download_id, {}).get("paused", False):
                        await asyncio.sleep(0.5)
                    # Resumed — reset speed tracking, reconnect stream from exact byte position
                    newly_downloaded = 0
                    start_time = time.time()
                    last_update = start_time
                    speed_samples = []
                    continue

                # Stream ended naturally — download complete
                break

        # ====== DOWNLOAD_COMPLETE LOG ======
        duration = int(time.time() - context.user_data.get("download_start_time", time.time()))
        log_to_db(
            vareon_id=user_data.get("vareon_id", "unknown"),
            tg_user_id=user_id,
            event_type="DOWNLOAD_COMPLETE",
            function_name="download_file_with_progress",
            task_id=context.user_data.get("task_id"),
            details={"time_taken": duration},
            action_status={"status": "success", "latency": f"{duration}s"}
        )
        logger.info(f"[DOWNLOAD] DOWNLOAD_COMPLETE logged | duration={duration}s")
        # ===================================

        used_bytes = get_folder_size(base_dir)
        free_bytes = max(0, STORAGE_QUOTA_BYTES - used_bytes)
        pct        = min(100.0, (used_bytes / STORAGE_QUOTA_BYTES) * 100)
        bar        = build_bar(pct)
        status     = get_status(pct)
        msg = (
            "✅ <b>Download complete!</b>\n\n"
            f"📁 <b>Filename:</b> <code>{filename}</code>\n"
            f"📍 <b>Location:</b> <code>{download_path}</code>\n\n"
            "<b>Storage Insight:-</b>\n"
            "<blockquote>"
            f"{bar}  <b>{pct:.2f}%</b>\n"
            f"├ Used  →  <b>{smart_format(used_bytes)}</b> of {STORAGE_QUOTA_LABEL}\n"
            f"├ Free  →  <b>{smart_format(free_bytes)}</b>\n"
            f"└ State →  {status}"
            "</blockquote>\n"
            "💡 Use /storage to view complete storage details\n"
        )
        await context.bot.edit_message_text(
            chat_id=query.message.chat_id,
            message_id=message_id,
            text=msg,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"❌ Download failed: {str(e)}\n{traceback.format_exc()}")

        # ====== DOWNLOAD_ERROR LOG ======
        if not download_status.get(download_id, {}).get('paused', False):
            duration = int(time.time() - context.user_data.get('download_start_time', time.time()))
            log_to_db(
                vareon_id=user_data.get('vareon_id', 'unknown'),
                tg_user_id=user_id,
                event_type='DOWNLOAD_ERROR',
                function_name='download_file_with_progress',
                task_id=context.user_data.get('task_id'),
                details={'time_taken': duration, 'error': str(e)},
                action_status={'status': 'error', 'latency': f'{duration}s'}
            )
            logger.info(f'[DOWNLOAD] DOWNLOAD_ERROR logged | duration={duration}s')
        # ================================

        if download_status.get(download_id, {}).get("paused", False):
            logger.info(f"Download paused by user. Download ID: {download_id}")
        else:
            keyboard = None
            if resume_supported and os.path.exists(path):
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔁 Try Resume", callback_data="resume_download", style="primary")]
                ])
            await context.bot.edit_message_text(
                chat_id=query.message.chat_id,
                message_id=message_id,
                text=f"❌ Error during download: {str(e)}",
                reply_markup=keyboard
            )
    finally:
        if not download_status.get(download_id, {}).get("paused", False):
            download_tasks.pop(download_id, None)
            download_status.pop(download_id, None)
            context.user_data.pop("current_download_id", None)
            context.user_data.pop("file_info", None)
            context.user_data.pop("resume_supported", None)


async def update_download_progress(update: Update, context: CallbackContext, message_id: int,
                                   downloaded: int, total: int, speed: float, start_time: float,
                                   download_id: str = None, filename: str = "unknown"):

    is_paused = download_status.get(download_id, {}).get("paused", False)
    if is_paused:
        text = (
            f"⏸️ **Download Paused**\n\n"
            f"📁 ` {filename} `\n"
            f"📊 Saved: {format_size(downloaded)} of {format_size(total)}\n\n"
            f"💡 *Click Resume below to continue downloading.*"
        )
    else:
        percent = (downloaded / total) * 100 if total > 0 else 0
        eta = (total - downloaded) / speed if speed > 0 else 0

        bar_length = 10
        filled = min(bar_length, int((percent / 100) * bar_length))
        progress_bar = "[" + "▪️" * filled + "▫️" * (bar_length - filled) + "]"
        action = "Uploading to Telegram" if download_id and "server_upload" in download_id else "Downloading to server"
        text = (
            f"{'⬆️' if 'Uploading' in action else '⬇️'} {action} `{filename}`\n\n"
            f"*Progress:* {percent:.1f}%\n"
            f"{progress_bar}\n"
            f"📊 {format_size(downloaded)} of {format_size(total)}\n"
            f"🚀 Speed: {format_speed(speed)}\n"
            f"⏳ ETA: {format_time(eta)}"
        )

    keyboard = []
    resume_supported = context.user_data.get("resume_supported", False)
    if resume_supported:
        keyboard.append([InlineKeyboardButton(
            "▶️ Resume" if is_paused else "⏸️ Pause",
            callback_data="resume_download" if is_paused else "pause_download", style="primary"
        )])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_process", style="danger")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    chat_id = update.message.chat_id if update.message else update.callback_query.message.chat_id
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )