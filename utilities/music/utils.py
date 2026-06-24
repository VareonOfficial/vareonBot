import asyncio
import re, os
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from main.config import logger
import time
from typing import Optional
from utilities.myfiles.upload import run_tdl_upload
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

async def upload_song_to_telegram(file_path, title, artist, user_id, upload_msg, context, vareon_id=None, task_id=None):
    upload_start = time.time()
    file_name = os.path.basename(file_path)
    tmp_dir = os.path.dirname(file_path)
    size_bytes = os.path.getsize(file_path) if os.path.exists(file_path) else 0

    try:
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            event_type="UPLOAD_STARTED",
            function_name="upload_song_to_telegram",
            task_id=task_id,
            details={"file_name": file_name, "title": title, "artist": artist, "size_bytes": size_bytes},
            action_status={"status": "in_progress"},
        )
        logger.info("[MUSIC] UPLOAD_STARTED | %s - %s", title, artist)

        await run_tdl_upload(
            progress_msg=upload_msg,
            path=tmp_dir,
            file_name=file_name,
            context=context,
            user_id=user_id,
            vareon_id=vareon_id,
        )

        upload_duration = int(time.time() - upload_start)
        size_mb = size_bytes / 1024 / 1024

        logger.info(
            "[MUSIC] ✅ Upload done | %s - %s | %.1fMB in %ds",
            title, artist, size_mb, upload_duration,
        )

        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            event_type="UPLOAD_COMPLETE",
            function_name="upload_song_to_telegram",
            task_id=task_id,
            details={
                "file_name": file_name,
                "title": title,
                "artist": artist,
                "size_bytes": size_bytes,
                "time_taken": upload_duration,
            },
            action_status={"status": "success", "latency": f"{upload_duration}s"},
        )
        logger.info("[MUSIC] UPLOAD_COMPLETE logged | duration=%ds", upload_duration)

        try:
            await upload_msg.delete()
        except Exception:
            pass

        try:
            os.remove(file_path)
        except Exception:
            pass

    except Exception as exc:
        upload_duration = int(time.time() - upload_start)
        logger.exception("[MUSIC] Upload failed:")
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            event_type="UPLOAD_ERROR",
            function_name="upload_song_to_telegram",
            task_id=task_id,
            details={"file_name": file_name, "title": title, "artist": artist, "error": str(exc), "time_taken": upload_duration},
            action_status={"status": "error", "latency": f"{upload_duration}s"},
        )
        try:
            await upload_msg.edit_text("❌ Upload failed. Please try again.")
        except Exception:
            pass
        raise