"""
upload_download.py
──────────────────
Handles Telegram file uploads via tdl.
Queue management is fully delegated to tdl_queue.py.
"""

import os
import time
import re
import uuid
import sqlite3
import asyncio
from telegram import InlineKeyboardMarkup
from main.config import logger, STORAGE_PATH, PRIVATE_GROUP_ID, VAREON_DB
from main.utils import format_size
from features.files.tdl_queue import queue_tdl_task


def _init_pending_uploads_table():
    """Ensure pending_uploads table exists."""
    conn = sqlite3.connect(VAREON_DB)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_uploads (
            uuid TEXT PRIMARY KEY,
            vareon_id TEXT NOT NULL,
            telegram_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

_init_pending_uploads_table()


async def run_tdl_upload(path, file_name, context, user_id, progress_msg=None, vareon_id=None):
    """
    Public entry-point for uploads.
    Prepares the file, then hands control to the shared tdl queue.
    """
    if progress_msg is None:
        progress_msg = await context.bot.send_message(
            chat_id=user_id,
            text=f"⬇️ Starting upload...\n`{file_name}`",
            parse_mode="Markdown",
        )
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

    # ── Generate UUID and register in DB ──────────────────────────────────────
    upload_uuid = str(uuid.uuid4())
    try:
        conn = sqlite3.connect(VAREON_DB)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO pending_uploads (uuid, vareon_id, telegram_id) VALUES (?, ?, ?)",
            (upload_uuid, vareon_id or "", str(user_id)),
        )
        conn.commit()
        conn.close()
        logger.info(f"[UPLOAD] Registered pending upload | uuid={upload_uuid} | user={user_id}")
    except Exception as e:
        logger.error(f"[UPLOAD] Failed to register pending upload in DB: {e}")
        await progress_msg.edit_text("❌ Failed to prepare upload. Please try again.")
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
            vareon_id=vareon_id,
            upload_uuid=upload_uuid,
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
    total_size, context, user_id, vareon_id, upload_uuid, cancel_btn,
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
        "--caption", f'"User: {user_id} | UUID: {upload_uuid}"',
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
            await _forward_uploaded_file(
                progress_msg=progress_msg,
                context=context,
                user_id=user_id,
                vareon_id=vareon_id,
                upload_uuid=upload_uuid,
                file_name=file_name,
            )
        else:
            # Clean up DB row since upload never completed
            try:
                conn = sqlite3.connect(VAREON_DB)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM pending_uploads WHERE uuid = ?", (upload_uuid,))
                conn.commit()
                conn.close()
            except Exception:
                pass

            error_msg = (
                "❌ Upload did not start (check tdl installation)"
                if not upload_started else "❌ Upload failed or was cancelled"
            )
            await progress_msg.edit_text(
                f"{error_msg}\n\n`{file_name}`\nExit code: {returncode}",
                parse_mode="Markdown",
            )


async def _forward_uploaded_file(progress_msg, context, user_id, vareon_id, upload_uuid, file_name):
    """
    Finds the just-uploaded message in the private group by UUID in caption,
    verifies it belongs to the right user via DB (telegram_id + vareon_id),
    forwards to user, deletes from group, and cleans up DB row.
    """
    logger.info(f"✅ Upload complete — locating message | uuid={upload_uuid}")

    try:
        import state
        u_client = state.telethon_user_client
        g_id = state.PRIVATE_GROUP_ID

        if not user_id:
            logger.error("user_id is None in _forward_uploaded_file")
            return

        # Wait briefly for Telegram to register the message
        await asyncio.sleep(3)

        # ── Search by UUID in caption — guaranteed unique, no filename issues ──
        found_msg_id = None

        async for message in u_client.iter_messages(g_id, limit=20):
            cap = message.message or ""
            if f"UUID: {upload_uuid}" in cap and f"User: {user_id}" in cap:
                found_msg_id = message.id
                break

        if found_msg_id is None:
            logger.error(f"[UPLOAD] Could not find message for uuid={upload_uuid} user={user_id}")
            # Clean up DB row
            try:
                conn = sqlite3.connect(VAREON_DB)
                cursor = conn.cursor()
                cursor.execute("DELETE FROM pending_uploads WHERE uuid = ?", (upload_uuid,))
                conn.commit()
                conn.close()
            except Exception:
                pass
            return

        # ── Verify ownership via DB (telegram_id + vareon_id double check) ────
        try:
            conn = sqlite3.connect(VAREON_DB)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT telegram_id, vareon_id FROM pending_uploads WHERE uuid = ?",
                (upload_uuid,),
            )
            row = cursor.fetchone()
            conn.close()
        except Exception as e:
            logger.error(f"[UPLOAD] DB verification failed: {e}")
            row = None

        if row is None:
            logger.error(f"[UPLOAD] UUID {upload_uuid} not found in DB — possible duplicate forward attempt")
            return

        db_telegram_id, db_vareon_id = row

        # Verify telegram_id matches
        if str(db_telegram_id) != str(user_id):
            logger.error(
                f"[UPLOAD] telegram_id mismatch! DB={db_telegram_id} vs actual={user_id} | uuid={upload_uuid}"
            )
            return

        # Verify vareon_id as 2nd layer
        if vareon_id and str(db_vareon_id) != str(vareon_id):
            logger.error(
                f"[UPLOAD] vareon_id mismatch! DB={db_vareon_id} vs actual={vareon_id} | uuid={upload_uuid}"
            )
            return

        logger.info(f"[UPLOAD] Ownership verified | uuid={upload_uuid} | user={user_id} | vareon={vareon_id}")

        # ── Forward to user via copy_message ──────────────────────────────────
        await context.bot.copy_message(
            chat_id=user_id,
            from_chat_id=g_id,
            message_id=found_msg_id,
            caption="",
        )
        logger.info(f"✅ Forwarded '{file_name}' (msg_id={found_msg_id}) to {user_id}")

        # ── Clean up group message ─────────────────────────────────────────────
        await u_client.delete_messages(g_id, found_msg_id)

        # ── Delete DB row — job done ───────────────────────────────────────────
        try:
            conn = sqlite3.connect(VAREON_DB)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM pending_uploads WHERE uuid = ?", (upload_uuid,))
            conn.commit()
            conn.close()
            logger.info(f"[UPLOAD] Cleared pending_uploads row | uuid={upload_uuid}")
        except Exception as e:
            logger.warning(f"[UPLOAD] Failed to delete DB row for uuid={upload_uuid}: {e}")

        try:
            await progress_msg.delete()
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Forward error: {e}", exc_info=True)
        # Best effort DB cleanup on unexpected error
        try:
            conn = sqlite3.connect(VAREON_DB)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM pending_uploads WHERE uuid = ?", (upload_uuid,))
            conn.commit()
            conn.close()
        except Exception:
            pass