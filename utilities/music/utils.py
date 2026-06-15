import asyncio
import re, os
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from main.config import logger
import time
from typing import Optional
from utilities.music.fast_telethon import upload_file as fast_upload_file
import state
from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeFilename
import mimetypes
from main.config import logger
from vareon_analytics.vr_log import log_to_db

import urllib.parse as up

async def validate_youtube_link(link: str, progress_msg) -> bool:
    parsed = up.urlparse(link)
    query = up.parse_qs(parsed.query)

    path = parsed.path
    list_id = query.get("list", [None])[0]

    # 🚫 MIX / RADIO
    if list_id and list_id.startswith(("RD", "RDEM", "RDAMVM")):
        await progress_msg.edit_text(
            "⚠️ *YouTube Mix detected* (mix/radio-type link).\n\n"
            "These are *dynamic* and cannot be fully downloaded.\n\n"
            "🔍 *How to identify it:*\n"
            "Look at your link:\n"
            f"`{link}`\n\n"
            "If you see something like:\n"
            "`list=RD...` → it's a Mix ❌\n"
            f"• From your link: `list={list_id}`\n\n"
            "`list=PL...` → it's a Playlist ✅\n\n"
            "👉 *Solution:*\n"
            "1. Open the mix in YouTube\n"
            "2. Save it as a playlist on your account\n"
            "3. Send the new playlist link\n\n"
            "Then I can download it properly.",
            parse_mode="Markdown"
        )
        return False

    # 🚫 FUTURE: PLAYABLES
    elif "playables" in link:
        await progress_msg.edit_text(
            "⚠️ Playables are not downloadable content."
        )
        return False

    # ✅ VALID
    return True

def extract_track_id(url: str):
    m = re.search(r"track/([A-Za-z0-9]+)", url)
    return m.group(1) if m else None

def build_cancel_keyboard(task_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_process|{task_id}", style="danger")]
    ])
def clean_filename(text):
    if not text:
        return ""
    return re.sub(r'[<>:"/\\|?*\n\r\t]', '', text).strip()

def clean_search(text):
    if not text:
        return ""
    return re.sub(r'[^\w\s&-]', '', text).strip()

def normalize_cookies(cookies):
    for c in cookies:
        if c.get("sameSite"):
            val = c["sameSite"].lower()
            if val in ["no_restriction", "unspecified"]:
                c["sameSite"] = "None"
            elif val == "lax":
                c["sameSite"] = "Lax"
            elif val == "strict":
                c["sameSite"] = "Strict"
        else:
            c["sameSite"] = "None"
    return cookies

def duration_to_seconds(duration_str):
    if not duration_str:
        return None
    parts = duration_str.strip().split(":")
    parts = [int(p) for p in parts]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None

progress_re = re.compile(
    r"\[download\]\s+([\d.]+)%\s+of\s+([\d.]+\w+)\s+at\s+([\d.]+\S+/s)\s+ETA\s+([\d:]+)"
)


def convert_to_si(value_str: str) -> str:
    """
    Converts any KiB/MiB/GiB → kB/MB/GB (base 1000).
    Works for both size and speed (e.g., '9.21MiB/s').
    """

    match = re.match(r"([\d.]+)\s*(KiB|MiB|GiB|B|kB|MB|GB)(/s)?", value_str)
    if not match:
        return value_str
    value, unit, per_sec = match.groups()
    value = float(value)
    # Convert everything → bytes
    to_bytes = {
        "B": 1,
        "kB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
    }

    bytes_val = value * to_bytes[unit]
    # Convert bytes → SI units
    if bytes_val >= 1000**3:
        new_val = bytes_val / (1000**3)
        new_unit = "GB"
    elif bytes_val >= 1000**2:
        new_val = bytes_val / (1000**2)
        new_unit = "MB"
    elif bytes_val >= 1000:
        new_val = bytes_val / 1000
        new_unit = "kB"
    else:
        new_val = bytes_val
        new_unit = "B"
    suffix = "/s" if per_sec else ""
    return f"{new_val:.2f}{new_unit}{suffix}"

# ═══════════════════════════════════════════════════════════════════════════════
# Progress helper
# ═══════════════════════════════════════════════════════════════════════════════

async def update_progress(
    msg,
    text: str,
    remove_keyboard: bool = False,
    task_id: Optional[str] = None,
    running_tasks: Optional[dict] = None,
) -> None:
    running_tasks = running_tasks or {}
    logger.debug("update_progress: task_id=%s remove_keyboard=%s", task_id, remove_keyboard)

    if task_id and running_tasks.get(task_id, {}).get("cancelling"):
        logger.info("Task %s cancelling — skipping progress update", task_id)
        return

    try:
        if remove_keyboard:
            await msg.edit_text(text, reply_markup=None, parse_mode="HTML", link_preview_options={"is_disabled": True})
        else:
            keyboard = (
                running_tasks[task_id].get("keyboard")
                if task_id and task_id in running_tasks
                else getattr(msg, "reply_markup", None)
            )
            await msg.edit_text(text, reply_markup=keyboard, parse_mode="HTML", link_preview_options={"is_disabled": True})
    except Exception as exc:
        logger.debug("update_progress edit_text skipped: %s", exc)

async def run_download(cmd, idx, total_tracks, progress_msg, task_id,
                       vareon_id=None, tg_user_id=None, tracks_completed=0):
    if "--newline" not in cmd:
        cmd.insert(1, "--newline")

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    start_time = asyncio.get_event_loop().time()

    last_update = 0
    converting_shown = False
    error_lines = []

    async def handle_stream(stream, is_stderr=False):
        nonlocal last_update, converting_shown

        while True:
            line = await stream.readline()
            if not line:
                break

            decoded = line.decode(errors="ignore").strip()

            if is_stderr:
                error_lines.append(decoded)
                if any(kw in decoded for kw in ["ERROR", "WARNING", "error", "failed"]):
                    logger.warning(f"[yt-dlp stderr] {decoded}")
                continue

            match = re.search(
                r"([\d.]+)%\s+of\s+([\d.]+\w+)\s+at\s+([\d.]+\w+/s)\s+ETA\s+([\d:]+)",
                decoded
            )

            if match:
                percent_str, total_size, speed, eta = match.groups()
                now = asyncio.get_event_loop().time()

                if now - last_update >= 2:
                    last_update = now
                    percent_f = float(percent_str)

                    filled = int(percent_f // 10)
                    bar = "▪️" * filled + "▫️" * (10 - filled)

                    # --- calculate downloaded ---
                    size_match = re.match(r"([\d.]+)(KiB|MiB|GiB|B|kB|MB|GB)", total_size)

                    if size_match:
                        size_val, size_unit = size_match.groups()
                        size_val = float(size_val)

                        unit_multipliers = {
                            "B": 1,
                            "kB": 1000,
                            "MB": 1000**2,
                            "GB": 1000**3,
                            "KiB": 1024,
                            "MiB": 1024**2,
                            "GiB": 1024**3,
                        }

                        total_bytes = size_val * unit_multipliers[size_unit]
                        downloaded_bytes = total_bytes * (percent_f / 100)

                        # convert both using SAME converter
                        downloaded_str = convert_to_si(f"{downloaded_bytes}B")
                        total_str = convert_to_si(total_size)

                        display_size = f"{downloaded_str} of {total_str}"
                    else:
                        display_size = convert_to_si(total_size)

                    display_speed = convert_to_si(speed)

                    text = (
                        f"<b>⬇️ Downloading [{idx}/{total_tracks}]</b>\n"
                        f"<b>Progress: {percent_str}%</b>\n\n"
                        f"<i>{bar}</i>\n\n"
                        f"📊 {display_size}\n"
                        f"🚀 Speed: {display_speed}\n"
                        f"⏳ ETA: {eta}"
                    )

                    await update_progress(progress_msg, text, task_id=task_id)

                continue

            if "[ExtractAudio]" in decoded and not converting_shown:
                converting_shown = True
                await update_progress(
                    progress_msg,
                    "✅ Download complete (100%)\n🔄 Converting audio & finalizing file...\nPlease wait...",
                    task_id=task_id
                )

    await asyncio.gather(
        handle_stream(process.stdout, is_stderr=False),
        handle_stream(process.stderr, is_stderr=True)
    )

    await process.wait()

    time_taken = round(asyncio.get_event_loop().time() - start_time, 2)
    error_text = "\n".join(error_lines)

    if process.returncode != 0:
        logger.error(f"[yt-dlp] rc={process.returncode}\n{error_text}")

        if vareon_id and tg_user_id:
            log_to_db(
                vareon_id=vareon_id,
                tg_user_id=tg_user_id,
                event_type="DOWNLOAD_ERROR",
                function_name="run_download",
                task_id=task_id,
                details={
                    "time_taken": time_taken,
                    "tracks_completed": tracks_completed if total_tracks > 1 else 0,
                    "track_index": idx,
                    "total_tracks": total_tracks,
                    "error_snippet": error_text[:300] if error_text else None,
                },
                action_status={"status": "error", "return_code": process.returncode},
            )
    return process.returncode, error_text

async def upload_song_to_telegram(chat_id, file_path, title, artist, user_id, upload_msg, context):
    last_print = [0]
    start_time = [time.time()]

    async def update_progress(current, total):
        now = time.time()
        if now - last_print[0] < 2:
            return
        last_print[0] = now

        percent   = (current / total) * 100
        bar       = "▪️" * int(percent // 10) + "▫️" * (10 - int(percent // 10))
        elapsed   = now - start_time[0]
        speed     = current / elapsed if elapsed > 0 else 0
        speed_str = f"{speed/1024/1024:.2f} MB/s" if speed >= 1024*1024 else f"{speed/1024:.1f} KB/s"
        eta       = (total - current) / speed if speed > 0 else 0
        eta_str   = f"{int(eta//3600):01}:{int((eta%3600)//60):02}:{int(eta%60):02}"
        nonlocal upload_msg
        if upload_msg:
            try:
                await upload_msg.edit_text(
                    f"⬆️ Uploading to Telegram\n"
                    f"Progress: {percent:.1f}%\n"
                    f"{bar}\n"
                    f"📊 {current/1024/1024:.2f}MB of {total/1024/1024:.2f}MB\n"
                    f"🚀 Speed: {speed_str}\n"
                    f"⏳ ETA: {eta_str}"
                )
            except:
                pass
        else:
            try:
                upload_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⬆️ Uploading to Telegram\n"
                        f"Progress: {percent:.1f}%\n"
                        f"{bar}\n"
                        f"📊 {current/1024/1024:.2f}MB of {total/1024/1024:.2f}MB\n"
                        f"🚀 Speed: {speed_str}\n"
                        f"⏳ ETA: {eta_str}"
                    )
                )
            except:
                upload_msg = None

    try:
        logger.info(f"Uploading: {title} - {artist}")

        filename  = os.path.basename(file_path)
        mime_type, _ = mimetypes.guess_type(file_path)
        mime_type = mime_type or "audio/mpeg"

        with open(file_path, "rb") as f:
            uploaded = await fast_upload_file(
                client=state.telethon_user_client,
                file=f,
                progress_callback=update_progress,
            )

        caption = f"{title}\nUser: {user_id}"
        sent = await state.telethon_user_client.send_file(
            entity=state.PRIVATE_GROUP_ID,
            file=uploaded,
            caption=caption,
            attributes=[
                DocumentAttributeAudio(duration=0, title=title, performer=artist),
                DocumentAttributeFilename(filename),
            ],
            mime_type=mime_type,
        )

        logger.info(f"✅ Uploaded to private group (msg_id={sent.id}), copying to user...")
        await asyncio.sleep(2)

        found_msg_id = None
        async for message in state.telethon_user_client.iter_messages(state.PRIVATE_GROUP_ID, limit=20):
            cap = message.message or ""
            if title in cap and f"User: {user_id}" in cap:
                found_msg_id = message.id
                break

        if found_msg_id:
            await context.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=state.PRIVATE_GROUP_ID,
                message_id=found_msg_id,
                caption="",
            )
            await state.telethon_user_client.delete_messages(state.PRIVATE_GROUP_ID, found_msg_id)
        else:
            logger.warning(f"Could not find uploaded message for user {user_id}")

        # ── Delete progress message after file is sent ───────────────
        try:
            await upload_msg.delete()
        except Exception:
            pass

        elapsed_total = time.time() - start_time[0]
        size_mb = os.path.getsize(file_path) / 1024 / 1024
        logger.info(f"✅ Done → {title} | {size_mb:.1f}MB in {elapsed_total:.1f}s @ {size_mb/elapsed_total:.2f} MB/s")
        os.remove(file_path)

    except Exception as e:
        try:
            await upload_msg.edit_text("❌ Upload failed. Please try again.")
        except Exception:
            pass
        logger.error(f"❌ Upload failed: {e}")
        raise