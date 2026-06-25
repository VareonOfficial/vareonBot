from main.config import logger
from telegram import Update
from telegram.ext import CallbackContext, ConversationHandler
from main.state import download_status, download_tasks, running_tasks, sessions
import asyncio, os, time
from features.cancel.active_states import cancel_active_state, clear_conv_handlers
from vareon_analytics.vr_log import log_to_db

# ── Import queue helpers ───────────────────────────────────────────────────────
from features.files.tdl_queue import is_user_in_queue, cancel_queued_user


async def respond(update: Update, text: str):
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text)
        except Exception:
            await update.callback_query.message.reply_text(text)
    elif update.message:
        await update.message.reply_text(text)
        
async def cancel_process(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    query = update.callback_query

    if query:
        await query.answer()

    data = update.callback_query.data if update.callback_query else ""
    task_id_from_button = data.split("|", 1)[1] if data.startswith("cancel_process|") else None

    logger.info(f"[CANCEL] User {user_id} initiated cancellation.")
    if clear_conv_handlers(context):
        await respond(
            update,
            "❌ Operation cancelled.\nAnything else I can do for you?"
        )
        return ConversationHandler.END 
        
    # ================= STATE CANCEL =================
    state_name = await cancel_active_state(user_id, context)
    if state_name:
        await respond(
            update,
            f"The command {state_name} has been cancelled.\nAnything else I can do for you?"
        )
        return
    # ================= MUSIC DOWNLOAD (YouTube / Spotify) =================
    if task_id_from_button and task_id_from_button in running_tasks:
        if await cancel_music_download(update, context, task_id_from_button, user_id):
            return

    # ================= TDL QUEUE (waiting, not yet running) ==================
    if is_user_in_queue(user_id):
        cancel_queued_user(user_id)
        await respond(update, "✅ Removed from queue. Your upload/download was cancelled.")
        return

    # ================= FILE DOWNLOAD =================
    active_process = context.user_data.get("active_process")
    if active_process:
        if await cancel_file_download(update, context, active_process, user_id):
            return

    # ================= FILE UPLOAD ===================
    active_process = context.user_data.get("active_process")
    if active_process:
        if await cancel_file_upload(update, context, active_process, user_id):
            return

    # ================= YT DOWNLOAD =================
    yt_process = context.user_data.get("active_yt_process")
    if yt_process:
        if await cancel_yt_download(update, context, yt_process, user_id):
            return

    # ================= LINK DOWNLOAD =================
    download_id = context.user_data.get("current_download_id")
    if download_id:
        if await cancel_link_download(update, context, download_id, user_id):
            return

    # ================= EXTRACTION & COMPRESSION (task_id based) =================
    data = update.callback_query.data if update.callback_query else ""
    task_id_from_button = data.split("|", 1)[1] if data.startswith("cancel_process|") else None
    is_extract_cancel = task_id_from_button and (
        context.user_data.get("active_extract_task_id") == task_id_from_button
        or context.user_data.get("multi_extract_cancel_id") == task_id_from_button
    )

    if is_extract_cancel:
        if await cancel_extraction(update, context, user_id):
            return
    elif task_id_from_button and context.user_data.get("active_compress_task_id") == task_id_from_button:
        if await cancel_compression(update, context):
            return
    elif not task_id_from_button:
        if context.user_data.get("active_extract_task_id"):
            if await cancel_extraction(update, context, user_id):
                return
        if context.user_data.get("active_compress_task_id"):
            if await cancel_compression(update, context):
                return
    # ================= FINAL =================
    await respond(
        update,
        "😴 Nothing to cancel. I wasn't doing anything anyway..."
    )

async def cancel_music_download(update: Update, context: CallbackContext, task_id: str, user_id: int) -> bool:
    if task_id not in running_tasks:
        return False

    task_info = running_tasks[task_id]

    if task_info.get("cancelling"):
        await respond(update, "⏳ Already cancelling, please wait...")
        return True

    task_info["cancelling"] = True
    logger.info(f"[CANCEL] Music task {task_id} flagged for cancellation by user {user_id}")

    asyncio_task = task_info.get("task")
    if asyncio_task and not asyncio_task.done():
        asyncio_task.cancel()

    process = task_info.get("process")
    if process and process.returncode is None:
        try:
            process.terminate()
        except Exception as e:
            logger.warning(f"[CANCEL] Could not terminate subprocess: {e}")

    running_tasks.pop(task_id, None)
    await respond(update, "⏹️ Music download cancelled.")
    return True

async def cancel_extraction(update, context, user_id):
    task_id = (
        update.callback_query.data.split("|", 1)[1]
        if update.callback_query and update.callback_query.data.startswith("cancel_process|")
        else context.user_data.get("active_extract_task_id")
    )
    if not task_id:
        return False

    context.user_data[f"extract_cancel_{task_id}"] = True
    logger.info(f"[CANCEL] Triggered extract_cancel_{task_id}")
    await respond(update, "⏹️ Extraction cancelled.")
    return True
async def cancel_compression(update, context):
    task_id = (
        update.callback_query.data.split("|", 1)[1]
        if update.callback_query and update.callback_query.data.startswith("cancel_process|")
        else context.user_data.get("active_compress_task_id")
    )
    if not task_id:
        return False

    context.user_data[f"compress_cancel_{task_id}"] = True
    logger.info(f"[CANCEL] Triggered compress_cancel_{task_id}")
    await respond(update, "⏹️ Compression cancelled.")
    return True

async def cancel_link_download(update, context, download_id: str, user_id: int):
    if not download_id or download_id not in download_status:
        return False
    
    file_info = context.user_data.get("file_info", {})
    filename = file_info.get("raw_name", "").strip()
    download_path = file_info.get("download_path")
    
    status = download_status[download_id]
    status["active"] = False
    status["paused"] = False

    task = download_tasks.get(download_id)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except:
            pass

    # ── File cleanup ──────────────────────────────────────────────────────────
    try:
        if filename and download_path:
            path = os.path.join(download_path, filename)
            if os.path.exists(path):
                os.remove(path)
                logger.info(f"[CANCEL] Deleted partial file: {path}")
            else:
                logger.warning(f"[CANCEL] File not found for deletion: {path}")
        else:
            logger.warning(f"[CANCEL] Could not determine file path — filename={filename!r}, download_path={download_path!r}")
    except Exception as e:
        logger.error(f"[CANCEL] File cleanup failed: {e}")

    # ── DOWNLOAD_CANCELED log ─────────────────────────────────────────────────
    try:
        vareon_id = sessions.get(user_id, {}).get("vareon_id", "unknown")
        duration = int(
            time.time()
            - context.user_data.get("download_start_time", time.time())
        )
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            event_type="DOWNLOAD_CANCELED",
            function_name="cancel_link_download",
            task_id=context.user_data.get("task_id"),
            details={"duration_seconds": duration, "canceled": True},
            action_status={"status": "canceled", "latency": f"{duration}s"}
        )
        logger.info(f"[CANCEL] DOWNLOAD_CANCELED logged | task_id={context.user_data.get('task_id')} | duration={duration}s")
    except Exception as e:
        logger.error(f"[CANCEL] Failed to log DOWNLOAD_CANCELED: {e}")
    # ─────────────────────────────────────────────────────────────────────────

    logger.info(f"[CANCEL] Link download cancelled for user {user_id}")

    download_tasks.pop(download_id, None)
    download_status.pop(download_id, None)
    context.user_data.pop("current_download_id", None)

    await respond(update, "✅ Download cancelled and cleaned up.")
    return True


async def cancel_yt_download(update, context, process, user_id: int):
    """Cancel an active yt-dlp subprocess download (cross-platform safe)"""
    try:
        context.user_data["yt_cancelled"] = True

        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("[CANCEL] Force killing yt-dlp for user %s", user_id)
                process.kill()
                await process.wait()
            logger.info("[CANCEL] yt-dlp process stopped for user %s", user_id)
        except Exception as e:
            logger.error("[CANCEL] Process termination error: %s", e)

        logger.info("[CANCEL] Cleanup path: %s", context.user_data.get("active_yt_dest_path"))
        cleanup_temp_files(context.user_data.get("active_yt_dest_path", ""))

        context.user_data.pop("active_yt_process", None)
        context.user_data.pop("active_yt_dest_path", None)
        context.user_data.pop("yt_cancelled", None)

        await respond(update, "✅ YouTube download cancelled and cleaned up.")
        logger.info("[CANCEL] Cancel complete for user %s", user_id)
        return True

    except Exception as e:
        logger.error("[CANCEL] cancel_yt_download error: %s", e)
        return False


async def cancel_file_download(update, context, process, user_id: int):
    try:
        context.user_data["tdl_cancelled"] = True

        task = context.user_data.pop("active_tdl_task", None)
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=3.0)
            except (asyncio.CancelledError, Exception):
                pass

        try:
            if process.returncode is None:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=3.0)
                logger.info(f"[CANCEL] tdl process killed for user {user_id}")
        except asyncio.TimeoutError:
            logger.warning(f"[CANCEL] Process did not exit in time for {user_id}")
        except Exception as e:
            logger.error(f"[CANCEL] Error killing process: {e}")

        logger.info("[CANCEL] Cleanup path: %s", context.user_data.get("active_download_path"))
        cleanup_temp_files(context.user_data.get("active_download_path", ""))

        context.user_data.pop("active_process", None)
        context.user_data.pop("active_download_path", None)
        context.user_data.pop("pending_download", None)
        context.user_data.pop("tdl_cancelled", None)

        await respond(update, "✅ File download cancelled and cleaned up.")
        logger.info(f"[CANCEL] File download cancelled for user {user_id}")
        return True

    except Exception as e:
        logger.error(f"[CANCEL] cancel_file_download error: {e}")
        return False


async def cancel_file_upload(update, context, process, user_id: int):
    try:
        context.user_data["tdl_cancelled"] = True

        task = context.user_data.pop("active_tdl_task", None)
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=3.0)
            except (asyncio.CancelledError, Exception):
                pass

        try:
            if process.returncode is None:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=3.0)
                logger.info(f"[CANCEL] tdl process killed for user {user_id}")
        except asyncio.TimeoutError:
            logger.warning(f"[CANCEL] Process did not exit in time for {user_id}")
        except Exception as e:
            logger.error(f"[CANCEL] Error killing process: {e}")

        logger.info("[CANCEL] Cleanup path: %s", context.user_data.get("active_upload_path"))
        cleanup_temp_files(context.user_data.get("active_upload_path", ""))

        context.user_data.pop("active_process", None)
        context.user_data.pop("active_upload_path", None)
        context.user_data.pop("tdl_cancelled", None)

        await respond(update, "✅ File upload cancelled and cleaned up.")
        logger.info(f"[CANCEL] File upload cancelled for user {user_id}")
        return True

    except Exception as e:
        logger.error(f"[CANCEL] cancel_file_upload error: {e}")
        return False


def cleanup_temp_files(*paths, patterns=(".part", ".tmp", ".ytdl")):
    """
    Deletes leftover temp files from given directories.
    Safe for Windows + Linux.
    """
    for path in paths:
        try:
            if not path or not os.path.exists(path):
                continue
            for f in os.listdir(path):
                try:
                    if f.endswith(patterns):
                        full_path = os.path.join(path, f)
                        if os.path.isfile(full_path):
                            os.remove(full_path)
                            logger.info("[CLEANUP] Deleted temp file: %s", f)
                except Exception as e:
                    logger.error("[CLEANUP] Failed deleting %s: %s", f, e)
        except Exception as e:
            logger.error("[CLEANUP] Directory error %s: %s", path, e)