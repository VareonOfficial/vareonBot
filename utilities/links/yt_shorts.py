import os
import re
import glob
import time
import asyncio
from main.config import logger, COOKIES_PATH, USERS_PATH
from main.state import sessions
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from vareon_analytics.vr_log import log_to_db
from utilities.myfiles.upload import run_tdl_upload

import shutil

def _get_executable(name):
    path = shutil.which(name)
    if not path:
        raise FileNotFoundError(f"Critical dependency '{name}' not found in system PATH.")
    return path

YTDLP_PATH  = _get_executable("yt-dlp")
FFMPEG_PATH = _get_executable("ffmpeg")

# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_shorts_url(url: str) -> bool:
    return bool(re.search(r"/shorts/", url, re.IGNORECASE))

def _parse_size_to_bytes(s: str) -> float:
    if not s:
        return 0
    s = s.replace("~", "").replace("≈", "").strip()
    units = {"KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}
    for u, f in units.items():
        if s.endswith(u):
            try:
                return float(s.replace(u, "")) * f
            except ValueError:
                return 0
    try:
        return float(s)
    except ValueError:
        return 0

def _fmt_size(b: float) -> str:
    if b >= 1024 ** 3:
        return f"{b / 1024 ** 3:.2f} GB"
    if b >= 1024 ** 2:
        return f"{b / 1024 ** 2:.2f} MB"
    if b >= 1024:
        return f"{b / 1024:.2f} KB"
    return f"{b:.0f} B"

# ── Button asks for Telegram Upload ─────────────────────────────────
async def show_shorts_upload_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Sends a message with an 'Upload Here' button.
    When clicked, triggers handle_shorts_download with the given URL.
    """
    chat_id = update.effective_chat.id

    # Store the URL so the callback can retrieve it
    context.user_data["shorts_url"] = True

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Upload Here", callback_data="download_here_tg", style="primary")]
    ])

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🎬 *YouTube Short detected!*\n\n"
            "Tap below to download and upload it directly to Telegram."
        ),
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return msg 

# ── Entry point ────────────────────────────────────────────────────────────────
async def handle_shorts_download(update: Update = None, context: ContextTypes.DEFAULT_TYPE = None):
    """
    Called when the user sends a YouTube Shorts link.
    Checks the URL, resolves the best format automatically, and kicks off the
    download + upload pipeline.
    """
    query   = update.callback_query
    user_id = query.from_user.id if query else update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Delete the folder menu message if it exists, to avoid clutter
    folder_menu_msg_id = context.user_data.pop("folder_menu_msg_id", None)
    if folder_menu_msg_id:
        try:
            await context.bot.delete_message(chat_id, folder_menu_msg_id)
        except Exception as e:
            logger.warning("[SHORTS] Could not delete folder menu msg: %s", e)


    url = context.user_data.get("link_url")
    if not url or not _is_shorts_url(url):
        text = "❌ No valid YouTube Shorts URL found."
        if query:
            await query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return

    vareon_id = sessions.get(user_id, {}).get("vareon_id")

    # ── Cookie check ──────────────────────────────────────────────────────────
    cookie_args = []
    if vareon_id:
        cookie_path = COOKIES_PATH / "youtube" / f"{vareon_id}.txt"
        if cookie_path.exists():
            cookie_args = ["--cookies", str(cookie_path)]
            logger.info("[SHORTS] Using cookie file: %s", cookie_path)
        else:
            logger.info("[SHORTS] No cookie file for vareon_id=%s, proceeding without cookies.", vareon_id)

    # ── Build .tmp destination path ───────────────────────────────────────────
    user_data   = sessions.get(user_id, {})
    base_dir    = f"{USERS_PATH}/{user_data.get('vareon_id')}"
    tmp_dir     = os.path.join(base_dir, ".tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    task_id = context.user_data.get("task_id")

    # Show a "starting" message so the user sees immediate feedback
    status_msg = None
    try:
        if query:
            await query.answer()
            status_msg = await query.edit_message_text(
                "⏳ Fetching best quality for Short… Please wait."
            )
        else:
            status_msg = await context.bot.send_message(
                chat_id=chat_id,
                text="⏳ Fetching best quality for Short… Please wait."
            )
    except Exception:
        pass

    # ── Log DOWNLOAD_STARTED ──────────────────────────────────────────────────
    context.user_data["download_start_time"] = time.time()
    log_to_db(
        vareon_id=vareon_id,
        tg_user_id=user_id,
        event_type="DOWNLOAD_STARTED",
        function_name="handle_shorts_download",
        task_id=task_id,
        details={"url": url, "type": "shorts"},
        action_status={"status": "in_progress"},
    )
    logger.info("[SHORTS] DOWNLOAD_STARTED | task_id=%s", task_id)

    # Fire the download in the background so we don't block the handler
    asyncio.create_task(
        _run_shorts_download(
            bot=context.bot,
            chat_id=chat_id,
            status_msg=status_msg,
            url=url,
            tmp_dir=tmp_dir,
            base_dir=base_dir,
            cookie_args=cookie_args,
            vareon_id=vareon_id,
            user_id=user_id,
            task_id=task_id,
            context=context,
        )
    )


# ── Download worker ────────────────────────────────────────────────────────────

async def _run_shorts_download(
    bot, chat_id, status_msg,
    url, tmp_dir, base_dir,
    cookie_args, vareon_id, user_id,
    task_id, context,
):
    """
    Downloads the Short (best video + best audio merged to mp4),
    streams live progress to Telegram, then uploads via fast Telethon.
    """
    filename_template = os.path.join(tmp_dir, "%(title)s.%(ext)s")

    # Format: video merged with audio → output mp4
    ytdlp_format = (
        "bestvideo[vcodec^=avc1][height<=1080]+bestaudio"
        "/best[height<=1080]"
    )

    cancel_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_process", style="danger")]]
    )

    # Regex that matches yt-dlp's live progress line
    progress_re = re.compile(
        r"\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+~?\s*([\d.]+[KMG]iB)"
        r"(?:\s+of\s+([\d.]+[KMG]iB))?"
        r"\s+at\s+([\d.]+[KMG]iB/s)"
        r"\s+ETA\s+([\d:]+)"
    )

    last_edit   = 0.0
    display_name = None
    total_bytes  = 0.0
    phase        = "video"   # tracks video vs audio phase
    seen_dest    = 0
    fmt_used     = ytdlp_format
    res_detected = "auto"

    try:
        proc = await asyncio.create_subprocess_exec(
            YTDLP_PATH,
            "-f", ytdlp_format,
            "--ffmpeg-location", FFMPEG_PATH,
            "--js-runtimes", "node",
            "--remote-components", "ejs:github",
            "--merge-output-format", "mp4",
            "--newline",
            "--no-warnings",
            *cookie_args,
            url,
            "-o", filename_template,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        if context:
            context.user_data["active_yt_process"]   = proc
            context.user_data["active_yt_dest_path"] = tmp_dir

        # ── Stream stdout line by line ─────────────────────────────────────
        async for raw_line in proc.stdout:
            # Check for user cancel
            if context and context.user_data.get("yt_cancelled"):
                try:
                    proc.terminate()
                except Exception:
                    pass
                break

            line = raw_line.decode(errors="ignore").strip()

            # Detect filename and phase (yt-dlp prints this before each segment)
            if line.startswith("[download] Destination:"):
                display_name  = os.path.basename(line.split(": ", 1)[-1])
                seen_dest    += 1
                phase         = "video" if seen_dest == 1 else "audio"

            # Detect resolution from [info] line e.g. "[info] 720x1280 ..."
            res_match = re.search(r"\b(\d{3,4}x\d{3,4})\b", line)
            if res_match and res_detected == "auto":
                res_detected = res_match.group(1)

            m = progress_re.search(line)
            if not m:
                continue

            percent_str, size_on_line, extra_total, speed_str, eta_str = m.groups()
            percent_float = float(percent_str)

            total_str  = extra_total or size_on_line
            total_bytes = _parse_size_to_bytes(total_str)
            downloaded  = (percent_float / 100) * total_bytes

            bar         = "▪️" * int(percent_float // 10) + "▫️" * (10 - int(percent_float // 10))
            phase_label = "🎥 Downloading Video" if phase == "video" else "🔊 Downloading Audio"

            progress_text = (
                f"{phase_label}\n"
                f"`{display_name or 'Processing...'}`\n\n"
                f"Progress: {percent_str}%\n"
                f"{bar}\n"
                f"{_fmt_size(downloaded)} of {_fmt_size(total_bytes)}\n"
                f"🚀 Speed: {speed_str}\n"
                f"⏳ ETA: {eta_str}"
            )

            now = time.time()
            if now - last_edit >= 2.0 and status_msg:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_msg.message_id,
                        text=progress_text,
                        parse_mode="Markdown",
                        reply_markup=cancel_markup,
                    )
                    last_edit = now
                except Exception:
                    pass

        await proc.wait()

        # ── Handle cancel ──────────────────────────────────────────────────
        if context and context.user_data.pop("yt_cancelled", None):
            duration = int(time.time() - context.user_data.get("download_start_time", time.time()))
            log_to_db(
                vareon_id=vareon_id,
                tg_user_id=user_id,
                event_type="DOWNLOAD_CANCELED",
                function_name="_run_shorts_download",
                task_id=task_id,
                details={"time_taken": duration, "canceled": True},
                action_status={"status": "canceled", "latency": f"{duration}s"},
            )
            logger.info("[SHORTS] DOWNLOAD_CANCELED | duration=%ds", duration)
            if status_msg:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_msg.message_id,
                        text="❌ Download canceled.",
                    )
                except Exception:
                    pass
            return

        # ── Check yt-dlp exit code ─────────────────────────────────────────
        if proc.returncode != 0:
            stderr_out = (await proc.stderr.read()).decode(errors="ignore")
            logger.error("[SHORTS] yt-dlp exited %d | %s", proc.returncode, stderr_out)
            if status_msg:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_msg.message_id,
                        text="❌ Download failed. yt-dlp returned an error.\nCheck logs for details.",
                    )
                except Exception:
                    pass
            return

        # ── Find the downloaded file ───────────────────────────────────────
        found = []
        for ext in ["mp4", "mkv", "webm", "mov", "avi"]:
            found.extend(glob.glob(os.path.join(tmp_dir, f"*.{ext}")))

        if not found:
            logger.error("[SHORTS] No output file found in %s", tmp_dir)
            if status_msg:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_msg.message_id,
                        text="❌ Download failed: no output file found.",
                    )
                except Exception:
                    pass
            return

        file_path   = max(found, key=os.path.getctime)
        file_name   = os.path.basename(file_path)
        file_size   = os.path.getsize(file_path)
        duration_dl = int(time.time() - context.user_data.get("download_start_time", time.time()))

        # ── Log FILE_INFO (post-download) ──────────────────────────────────
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            event_type="FILE_INFO",
            function_name="_run_shorts_download",
            task_id=task_id,
            details={
                "file_name":   file_name,
                "size_bytes":  file_size,
                "path":        file_path,
                "fmt":         fmt_used,
                "resolution":  res_detected,
            },
            action_status={"status": "in_progress"},
        )
        logger.info("[SHORTS] FILE_INFO logged | %s | %s", file_name, _fmt_size(file_size))

        # ── Log DOWNLOAD_COMPLETE ──────────────────────────────────────────
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            event_type="DOWNLOAD_COMPLETE",
            function_name="_run_shorts_download",
            task_id=task_id,
            details={"time_taken": duration_dl, "file_name": file_name},
            action_status={"status": "success", "latency": f"{duration_dl}s"},
        )
        logger.info("[SHORTS] DOWNLOAD_COMPLETE | duration=%ds", duration_dl)

        # ── Upload via fast Telethon ───────────────────────────────────────
        await _upload_short_to_telegram(
            bot=bot,
            chat_id=chat_id,
            status_msg=status_msg,
            file_path=file_path,
            file_name=file_name,
            vareon_id=vareon_id,
            user_id=user_id,
            task_id=task_id,
            context=context,
        )

    except Exception as exc:
        logger.exception("[SHORTS] Critical error during download:")
        duration = int(time.time() - context.user_data.get("download_start_time", time.time()))
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            event_type="DOWNLOAD_ERROR",
            function_name="_run_shorts_download",
            task_id=task_id,
            details={"error": str(exc), "time_taken": duration},
            action_status={"status": "error", "latency": f"{duration}s"},
        )
        if status_msg:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg.message_id,
                    text=f"❌ Error during download:\n`{exc}`",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
    finally:
        context.user_data.pop("active_yt_process",   None)
        context.user_data.pop("active_yt_dest_path", None)
        context.user_data.pop("yt_cancelled",         None)


# ── Upload worker ──────────────────────────────────────────────────────────────

async def _upload_short_to_telegram(
    bot, chat_id, status_msg,
    file_path, file_name,
    vareon_id, user_id,
    task_id, context,
):
    """
    Uploads the downloaded Short via tdl (same as the regular upload flow),
    then logs the result to DB and cleans up the tmp file.
    """
    upload_start = time.time()

    try:
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            event_type="UPLOAD_STARTED",
            function_name="_upload_short_to_telegram",
            task_id=task_id,
            details={"file_name": file_name, "size_bytes": os.path.getsize(file_path)},
            action_status={"status": "in_progress"},
        )
        logger.info("[SHORTS] UPLOAD_STARTED | %s", file_name)

        tmp_dir = os.path.dirname(file_path)

        # run_tdl_upload handles progress display, group upload, forward to user,
        # and cleanup of the group message — same as the regular file upload flow
        await run_tdl_upload(
            progress_msg=status_msg,
            path=tmp_dir,
            file_name=file_name,
            context=context,
            user_id=user_id,
        )

        upload_duration = int(time.time() - upload_start)
        size_mb = os.path.getsize(file_path) / 1024 / 1024 if os.path.exists(file_path) else 0

        logger.info(
            "[SHORTS] ✅ Upload done | %s | %.1fMB in %ds",
            file_name, size_mb, upload_duration,
        )

        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            event_type="UPLOAD_COMPLETE",
            function_name="_upload_short_to_telegram",
            task_id=task_id,
            details={
                "file_name":  file_name,
                "size_bytes": int(size_mb * 1024 * 1024),
                "time_taken": upload_duration,
            },
            action_status={"status": "success", "latency": f"{upload_duration}s"},
        )
        logger.info("[SHORTS] UPLOAD_COMPLETE logged | duration=%ds", upload_duration)

        # Clean up tmp file
        try:
            os.remove(file_path)
        except Exception:
            pass

    except Exception as exc:
        logger.exception("[SHORTS] Upload failed:")
        upload_duration = int(time.time() - upload_start)
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            event_type="UPLOAD_ERROR",
            function_name="_upload_short_to_telegram",
            task_id=task_id,
            details={"error": str(exc), "time_taken": upload_duration},
            action_status={"status": "error", "latency": f"{upload_duration}s"},
        )
        if status_msg:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg.message_id,
                    text="❌ Upload failed. Please try again.",
                )
            except Exception:
                pass
        raise