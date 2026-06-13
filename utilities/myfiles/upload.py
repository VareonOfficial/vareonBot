"""
upload_download.py
──────────────────
Handles Telegram file uploads via tdl.
Queue management is fully delegated to tdl_queue.py.
"""

import os
import time
import re
import asyncio
from telegram import InlineKeyboardMarkup
from main.config import logger, STORAGE_PATH, PRIVATE_GROUP_ID
from main.utils import format_size
from utilities.files.tdl_queue import queue_tdl_task


async def run_tdl_upload(progress_msg, path, file_name, context, user_id):
    """
    Public entry-point for uploads.
    Prepares the file, then hands control to the shared tdl queue.
    """
    file_path = os.path.join(path, file_name)

    # ── File size ──────────────────────────────────────────────────────────────
    try:
        total_size = format_size(os.path.getsize(file_path))
    except Exception as e:
        logger.warning(f"Could not get file size: {e}")
        total_size = None

    # ── Smart rename: strip commas to avoid tdl CLI errors ────────────────────
    original_file_path = os.path.normpath(file_path)
    temp_file_name = file_name.replace(",", "_") if "," in file_name else file_name
    temp_file_path = os.path.normpath(os.path.join(path, temp_file_name))

    try:
        if original_file_path != temp_file_path:
            os.rename(original_file_path, temp_file_path)
            logger.info(f"Renamed '{file_name}' → '{temp_file_name}' for upload")
    except Exception as e:
        logger.error(f"Failed to rename file before upload: {e}")
        await progress_msg.edit_text("❌ Failed to prepare file for upload.")
        return

    # ── Hand off to the shared queue ──────────────────────────────────────────
    async def _upload_task(cancel_btn: InlineKeyboardMarkup):
        await _do_upload(
            progress_msg=progress_msg,
            path=path,
            file_name=file_name,
            original_file_path=original_file_path,
            temp_file_name=temp_file_name,
            temp_file_path=temp_file_path,
            total_size=total_size,
            context=context,
            user_id=user_id,
            cancel_btn=cancel_btn,
        )

    await queue_tdl_task(
        progress_msg=progress_msg,
        context=context,
        user_id=user_id,
        file_name=file_name,
        kind="upload",
        task_fn=_upload_task,
    )


# ── Private: actual upload logic (runs only when lock is held) ────────────────

async def _do_upload(
    progress_msg, path, file_name,
    original_file_path, temp_file_name, temp_file_path,
    total_size, context, user_id, cancel_btn,
):
    GROUP_ID = str(PRIVATE_GROUP_ID).replace("-100", "")

    cmd = [
        "tdl", "up",
        "-p", temp_file_path,
        "-c", GROUP_ID,
        "--storage", f"type=bolt,path={STORAGE_PATH}",
        "--threads", "16",
        "--pool", "0",
        "--limit", "8",
        "--caption", f'"User: {user_id} | " + FileName',
    ]

    logger.info(f"Starting upload: {temp_file_name} (original: {file_name})")
    logger.info(f"Command: {' '.join(cmd)}")

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    context.user_data["active_process"] = process
    context.user_data["active_upload_path"] = path

    percent = 0.0
    downloaded = "0B"
    speed = "0B/s"
    eta = "N/A"
    last_update = 0
    upload_started = False

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

            upload_started = True
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
                        f"⬇️ *Uploading*\n"
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
        logger.info("Upload task cancelled")
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
        logger.info(f"tdl upload exited with code: {returncode}")

        # Restore original filename if it was temporarily renamed
        try:
            if temp_file_path != original_file_path and os.path.exists(temp_file_path):
                os.rename(temp_file_path, original_file_path)
                logger.info(f"Restored '{temp_file_name}' → '{file_name}'")
        except Exception as e:
            logger.error(f"Failed to restore original filename: {e}")

        if returncode == 0 and upload_started:
            await _forward_uploaded_file(progress_msg, context, user_id, file_name)
        else:
            error_msg = (
                "❌ Upload did not start (check tdl installation)"
                if not upload_started else "❌ Upload failed or was cancelled"
            )
            await progress_msg.edit_text(
                f"{error_msg}\n\n`{file_name}`\nExit code: {returncode}",
                parse_mode="Markdown",
            )


async def _forward_uploaded_file(progress_msg, context, user_id, file_name):
    """
    Finds the just-uploaded message in the private group and forwards it to
    the user, then deletes it from the group.

    Strategy:
    - Use Telethon (iter_messages) to find the message — no pyro_user_client needed.
    - Use copy_message for ALL media types (document, video, audio, animation).
      send_document was causing 'Wrong file identifier' because tdl uploads
      .mp4 files as Video type, and PTB rejects video file_ids in send_document.
    - copy_message works regardless of media type and preserves the file as-is.
    """
    logger.info("✅ Upload complete — locating message in group...")

    try:
        import state  # This is not an error if it shows in Yellow colour
        u_client = state.telethon_user_client
        g_id = state.PRIVATE_GROUP_ID
        user_chat_id = user_id

        if not user_chat_id:
            logger.error("user_chat_id not found in context")
            await progress_msg.edit_text(
                "Oops, we hit a snag! 🛠\n\n"
                "Please let the team know by sending a `/report` with a quick "
                "description so we can get this sorted for you.",
                parse_mode="Markdown",
            )
            return

        # Wait briefly for Telegram to register the message
        await asyncio.sleep(3)

        # Scan recent messages by recency — no search index needed
        found_msg_id = None
        file_name_no_ext = os.path.splitext(file_name)[0]

        async for message in u_client.iter_messages(g_id, limit=20):
            cap = message.message or ""  # Telethon uses .message not .caption
            if file_name_no_ext in cap and f"User: {user_id}" in cap:
                found_msg_id = message.id
                break

        if found_msg_id is None:
            logger.error(f"Could not find uploaded message for {user_id} after retries")
            await progress_msg.edit_text(
                "Oops, we hit a snag! 🛠\n\n"
                "Please let the team know by sending a `/report` with a quick "
                "description so we can get this sorted for you.",
                parse_mode="Markdown",
            )
            return

        # ── Forward to user via copy_message ──────────────────────────────────
        # copy_message works for ALL media types (document, video, audio, etc.)
        # and avoids the 'Wrong file identifier' error that send_document raises
        # when given a video/audio file_id instead of a document file_id.
        await context.bot.copy_message(
            chat_id=user_chat_id,
            from_chat_id=g_id,
            message_id=found_msg_id,
            caption="",
        )
        logger.info(f"✅ Forwarded '{file_name}' (msg_id={found_msg_id}) to {user_id}")

        # ── Clean up group message ─────────────────────────────────────────────
        await u_client.delete_messages(g_id, found_msg_id)
        try:
            await progress_msg.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Forward error: {e}", exc_info=True)
        try:
            await progress_msg.edit_text(
                "Oops, we hit a snag! 🛠️\n\n"
                "Please let the team know by sending a `/report` with a quick "
                "description so we can get this sorted for you.",
                parse_mode="Markdown",
            )
        except Exception:
            pass