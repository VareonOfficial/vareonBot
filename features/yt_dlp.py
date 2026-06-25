from fileinput import filename
import os, shutil
import re
import time
import glob
import asyncio
from main.config import logger, COOKIES_PATH, USERS_PATH
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from main.state import sessions
from vareon_analytics.vr_log import log_to_db
from features.storage import get_folder_size, STORAGE_QUOTA_BYTES, get_status, build_bar, smart_format, STORAGE_QUOTA_LABEL

    
def get_executable(name):
    """Finds the absolute path of an executable on any OS."""
    path = shutil.which(name)
    if not path:
        raise FileNotFoundError(f"Critical dependency '{name}' not found in system PATH.")
    return path

# Usage
YTDLP_PATH = get_executable("yt-dlp")
FFMPEG_PATH = get_executable("ffmpeg")

################################
# Youtube Download Handlers
################################
async def show_youtube_quality_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    old_message = query.message if query else None
    chat_id = old_message.chat_id if old_message else update.effective_chat.id
    
    await update.callback_query.edit_message_text("⏳ Fetching available formats from YouTube... Please wait.")
    
    url = context.user_data.get("link_url")
    if not url:
        if update.callback_query:
            await update.callback_query.edit_message_text("❌ No YouTube URL found.")
        else:
            await update.message.reply_text("❌ No YouTube URL found.")
        return

    current_path = context.user_data.get("selected_download_path")
    if not current_path:
        file_info = context.user_data.get("file_info", {})
        current_path = file_info.get("download_path", None)

    # ── COOKIE LOGIC ───────────────────────────────────────
    cookie_args = []
    vareon_id = sessions.get(query.from_user.id, {}).get("vareon_id")
    if vareon_id:
        cookie_path = COOKIES_PATH / "youtube" / f"{vareon_id}.txt"
        if cookie_path.exists():
            cookie_args = ["--cookies", str(cookie_path)]
            logger.info("[YT-DLP] Using cookie file: %s", cookie_path)
        else:
            logger.info("[YT-DLP] No cookie file found for vareon_id=%s. Prompting user.", vareon_id)
            await update.callback_query.edit_message_text(
                text=(
                    "🍪 **Cookies Required**\n\n"
                    "To fetch formats and ensure high-speed downloads, please set your YouTube cookies first:\n\n"
                    "1. Run the command /cookies\n"
                    "2. Follow the instructions to upload your `.txt` cookie file.\n"
                    "3. Once done, come back here and try again!\n\n"
                ),
                parse_mode="Markdown"
            )
            return
        
    retries = 3
    delay = 1.5
    formats = []
    for attempt in range(retries):
        try:
            args = [
                "-F", url,
                "--js-runtimes", "node",
                "--remote-components", "ejs:github",
                "--no-warnings",
                *cookie_args,
            ]

            # Use EXEC instead of SHELL to prevent injection
            proc = await asyncio.create_subprocess_exec(
                YTDLP_PATH,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy()
            )

            out, err = await proc.communicate()
            
            if proc.returncode != 0:
                await asyncio.sleep(delay)
                continue

            formats = out.decode(errors="ignore").splitlines()
            format_lines = [line for line in formats if re.match(r'^\d+\s', line)]
            
            if format_lines:
                break
                
            await asyncio.sleep(delay)

        except Exception as e:
            await asyncio.sleep(delay)

    # --- NEW LOGIC: Always pick the largest file for each resolution, tested or untested ---
    resolutions = ['2160', '1440', '1080', '720', '480', '360', '240']
    formats_by_res = {r: [] for r in resolutions}
    audio_formats = []
    def parse_size(size):
        if not size:
            return 0
        try:
            if 'GiB' in size:
                return float(size.replace('GiB','').replace('~','').replace('≈','').strip()) * 1024 * 1024 * 1024
            if 'MiB' in size:
                return float(size.replace('MiB','').replace('~','').replace('≈','').strip()) * 1024 * 1024
        except:
            return 0
        return 0

    for line in format_lines:
        parts = line.split()
        fmt_id = parts[0]
        ext = parts[1] if len(parts) > 1 else ''
        res = next((p for p in parts if re.match(r'\d{3,5}x\d{3,5}', p)), None)
        size = next((p for p in parts if re.match(r'\d+(\.\d+)?(MiB|GiB)', p)), None)
        more = ' '.join(parts)
        if 'audio only' in line:
            audio_formats.append({'id': fmt_id, 'ext': ext, 'size': size, 'size_bytes': parse_size(size), 'line': line, 'more': more})
            continue
        if res:
            m = re.match(r'(\d{3,5})x(\d{3,5})', res)
            if m:
                height = m.group(2)
                if height in formats_by_res:
                    formats_by_res[height].append({'id': fmt_id, 'ext': ext, 'size': size, 'size_bytes': parse_size(size), 'line': line, 'more': more, 'res': res})

    quality_buttons = []
    # For each resolution, pick the largest (tested or untested)
    for r in resolutions:
        if not formats_by_res[r]:
            continue
        best = max(formats_by_res[r], key=lambda f: f['size_bytes'])
        label = f"{r}p"
        if best.get('res'):
            label += f" - {best['res']}"
        if best.get('size'):
            label += f" | {best['size']}"
        if 'untested' in best['more'].lower():
            label += " (untested)"
        # Remove (video only) and (audio only) from label
        # Store the format id and resolution for callback
        quality_buttons.append([InlineKeyboardButton(label, callback_data=f"yt_quality|{best['id']}|{r}")])

    # If no standard formats, show all available formats (including Untested/Premium/m3u8) as fallback
    if not quality_buttons:
        fallback_buttons = []
        for line in formats:
            if not line.strip() or 'storyboard' in line or 'images' in line or line.strip().startswith('ID'):
                continue
            if re.match(r'^\d+', line.strip()):
                parts = line.split()
                fmt_id = parts[0]
                ext = parts[1] if len(parts) > 1 else ''
                res = next((p for p in parts if 'x' in p and p.replace('x','').isdigit()), None)
                size = next((p for p in parts if p.endswith('MiB') or p.endswith('GiB')), None)
                label = f"{fmt_id} {ext}"
                if res:
                    label += f" - {res}"
                if size:
                    label += f" | {size}"
                if 'untested' in line.lower() or 'premium' in line.lower() or 'm3u8' in line.lower():
                    label += " ⚠️"
                fallback_buttons.append([InlineKeyboardButton(label, callback_data=f"yt_quality|{fmt_id}")])
        if fallback_buttons:
            fallback_buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_process", style="danger")])
            warn_text = (
                "⚠️ Only untested or premium/m3u8 formats are available for this video. "
                "These may not download correctly or may require a premium account.\n\n"
                f"✅ Download location updated to `{current_path}`\n\n 🎥 Now select the quality for download:"
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=warn_text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(fallback_buttons)
            )
            return
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ No suitable formats found for this video after several attempts. It may be protected, unavailable, or YouTube has restricted downloads."
            )
            return

    quality_buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_process", style="danger")])
    msg_text = f"✅ Download location updated to `{current_path}`\n\n 🎥 Now select the quality for download:"
    await context.bot.send_message(
        chat_id=chat_id,
        text=msg_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(quality_buttons)
    )
    if old_message:
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=old_message.message_id
            )
        except Exception as e:
            logger.warning("[DELETE FAILED] %s", e)
        
def parse_size_to_bytes(s):
    if not s: return 0
    s = s.replace('~', '').replace('≈', '').strip()
    units = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
    for u, f in units.items():
        if s.endswith(u):
            return float(s.replace(u, "")) * f
    return float(s)

def fmt_size(b):
    if b >= 1024**3: return f"{b/1024**3:.2f} GB"
    if b >= 1024**2: return f"{b/1024**2:.2f} MB"
    if b >= 1024: return f"{b/1024:.2f} KB"
    return f"{b:.2f} B"

async def youtube_quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, fmt_id, res = query.data.split("|", 2)

    url = context.user_data.get("link_url")
    dest_path = context.user_data.get("selected_download_path")
    filename_template = os.path.join(dest_path, "%(title)s.%(ext)s")
    user_id = query.from_user.id
    vareon_id = sessions.get(user_id, {}).get("vareon_id")

    msg = await query.edit_message_text(
        "⬇️ Starting YouTube download...\nProgress will be shown below."
    )

    # ====================== DOWNLOAD_STARTED LOG ======================
    context.user_data["download_start_time"] = time.time()
    log_to_db(
        vareon_id=vareon_id,
        tg_user_id=user_id,
        event_type="DOWNLOAD_STARTED",
        function_name="youtube_quality_callback",
        task_id=context.user_data.get("task_id"),
        details={"fmt_id": fmt_id, "resolution": res},
        action_status={"status": "in_progress"}
    )
    logger.info(f"[YT] DOWNLOAD_STARTED logged | task_id={context.user_data.get('task_id')}")
    # ==================================================================
    asyncio.create_task(
        start_youtube_download(
            context.bot, msg.chat_id, msg.message_id,
            url, dest_path, filename_template, fmt_id, res, context,
            vareon_id=vareon_id, user_id=user_id
        )
    )
  
async def start_youtube_download(
    bot, chat_id, message_id, url, dest_path,
    filename_template, fmt_id, res, context=None,
    vareon_id=None, user_id=0
):
    is_playlist = bool(re.search(r"(list=|/playlist)", url, re.IGNORECASE))
    try:
        ytdlp_format = f"{fmt_id}+bestaudio/best"

        # ── COOKIE LOGIC ───────────────────────────────────────
        cookie_args = []
        if vareon_id:
            cookie_path = COOKIES_PATH / "youtube" / f"{vareon_id}.txt"
            if cookie_path.exists():
                cookie_args = ["--cookies", str(cookie_path)]
                logger.info("[YT-DLP] Using cookie file: %s", cookie_path)
            else:
                logger.info("[YT-DLP] No cookie file found for vareon_id=%s, running without cookies", vareon_id)

        proc = await asyncio.create_subprocess_exec(
            YTDLP_PATH,
            "-f", ytdlp_format,
            "--ffmpeg-location", FFMPEG_PATH,
            "--js-runtimes", "node",
            "--remote-components", "ejs:github",
            *cookie_args,
            url,
            "-o", filename_template,
            "--newline",
            "--merge-output-format", "mp4",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        if context:
            context.user_data["active_yt_process"] = proc
            context.user_data["active_yt_dest_path"] = dest_path

        progress_re = re.compile(
            r"\[download\]\s+(\d+\.\d+)% of\s+~?\s*([\d\.]+[KMG]iB)(?: of ([\d\.]+[KMG]iB))? at\s+([\d\.]+[KMG]iB/s) ETA ([\d:]+)"
        )
        last_update = 0
        display_name = None
        downloaded_bytes = 0
        total_bytes = None
        percent_float = 0.0
        phase = "video"
        seen_destination = 0
        cancel_button = InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_process", style="danger")]]
        )

        async for line in proc.stdout:
            if context and context.user_data.get("yt_cancelled"):
                break
            decoded = line.decode(errors="ignore").strip()
            
            if decoded.startswith("[download] Destination: "):
                display_name = os.path.basename(decoded.split(": ", 1)[-1])

                seen_destination += 1
                if seen_destination == 1:
                    phase = "video"
                else:
                    phase = "audio"
                    
            m = progress_re.search(decoded)
            if m:
                percent, size_on_line, extra_total, speed, eta_yt = m.groups()
                percent_float = float(percent)

                # Determine Total and Calculate Downloaded
                total_str = extra_total if extra_total else size_on_line
                total_bytes = parse_size_to_bytes(total_str)
                downloaded_bytes = (percent_float / 100) * total_bytes
                
                size_display = f"{fmt_size(downloaded_bytes)} of {fmt_size(total_bytes)}"
                filled_blocks = int(percent_float // 10)
                bar = "▪️" * filled_blocks + "▫️" * (10 - filled_blocks)
                phase_text = "🎥 *Downloading Video*" if phase == "video" else "🔊 *Downloading Audio*"
                
                progress_msg = (
                    f"{phase_text}\n"
                    f"`{display_name if display_name else 'Processing...'}`\n\n"
                    f"*Progress:* {percent}%\n"
                    f"{bar}\n"
                    f"*{size_display}*\n"
                    f"🚀 *Speed:* {speed}\n"
                    f"⏳ *ETA:* {eta_yt}"
                )

                now = time.time()
                if now - last_update > 2.0:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=progress_msg,
                            parse_mode="Markdown",
                            reply_markup=cancel_button
                        )
                        last_update = now
                    except Exception:
                        pass

        # wait for process to finish
        await proc.wait()
        stderr = await proc.stderr.read()
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            event_type="FILE_INFO",
            function_name="youtube_quality_callback",
            task_id=context.user_data.get("task_id"),
            details={
                "path": dest_path,
                "file_name": display_name,
                "size_bytes": total_bytes,
            },
            action_status={"status": "in_progress"}
        )
        logger.info(f"[YT] FILE_INFO logged | task_id={context.user_data.get('task_id')}")
        # ==================================================================

        flag = context.user_data.pop("yt_cancelled", None)
        if flag is True:
            # ====== DOWNLOAD_CANCELED LOG ======
            task_id = context.user_data.get('task_id') if context else None
            duration = int(time.time() - context.user_data.get('download_start_time', time.time())) if context else 0
            log_to_db(
                vareon_id=vareon_id,
                tg_user_id=user_id,
                event_type='DOWNLOAD_CANCELED',
                function_name='start_youtube_download',
                task_id=task_id,
                details={'time_taken': duration, 'canceled': True},
                action_status={'status': 'canceled', 'latency': f'{duration}s'}
            )
            logger.info(f'[YT] DOWNLOAD_CANCELED logged | duration={duration}s')
            # =====================================
            return
        
        if proc.returncode != 0:
            err_msg = stderr.decode(errors="ignore")
            logger.error(f"yt-dlp process exited with code {proc.returncode}")
            logger.error(f"Full Error Log: {err_msg}")

            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="❌ yt-dlp failed.\nCheck logs for details."
            )
            return

        logger.info("yt-dlp process finished successfully. Searching for files...")

        # find downloaded file
        found = []
        exts = ["mp4", "mkv", "webm", "mp3", "m4a", "mov", "avi"]

        for ext in exts:
            found.extend(glob.glob(os.path.join(dest_path, f"*.{ext}")))

        if not found:
            logger.error(f"No files found in {dest_path} after successful download.")

            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="❌ Download failed: No output file found."
            )
            return

        latest_file = max(found, key=os.path.getctime)

        if is_playlist:
            final_text = (
                "✅ <b>✅ Playlist downloaded!</b>\n\n"
                f"📁 <b>Filename:</b> <code>{os.path.basename(latest_file)}</code>\n"
                f"📍 <b>Location:</b> <code>{dest_path}</code>\n\n"
                "<b>Storage Insight:-</b>\n"
                "<blockquote>"
                f"{bar}  <b>{pct:.2f}%</b>\n"
                f"├ Used  →  <b>{smart_format(used_bytes)}</b> of {STORAGE_QUOTA_LABEL}\n"
                f"├ Free  →  <b>{smart_format(free_bytes)}</b>\n"
                f"└ State →  {status}"
                "</blockquote>\n"
                "💡 Use /storage to view complete storage details\n"
            ) 
        else:
            latest_file = max(found, key=os.path.getctime)
            user_data = sessions.get(user_id)
            base_dir = f"{USERS_PATH}/{user_data.get("vareon_id")}"
            used_bytes = get_folder_size(base_dir)
            free_bytes = max(0, STORAGE_QUOTA_BYTES - used_bytes)
            pct        = min(100.0, (used_bytes / STORAGE_QUOTA_BYTES) * 100)
            bar        = build_bar(pct)
            status     = get_status(pct)
            final_text = (
                "✅ <b>Download complete!</b>\n\n"
                f"📁 <b>Filename:</b> <code>{os.path.basename(latest_file)}</code>\n"
                f"📍 <b>Location:</b> <code>{dest_path}</code>\n\n"
                "<b>Storage Insight:-</b>\n"
                "<blockquote>"
                f"{bar}  <b>{pct:.2f}%</b>\n"
                f"├ Used  →  <b>{smart_format(used_bytes)}</b> of {STORAGE_QUOTA_LABEL}\n"
                f"├ Free  →  <b>{smart_format(free_bytes)}</b>\n"
                f"└ State →  {status}"
                "</blockquote>\n"
                "💡 Use /storage to view complete storage details\n"
            ) 

        # ====== DOWNLOAD_COMPLETE LOG ======
        task_id = context.user_data.get('task_id') if context else None
        duration = int(time.time() - context.user_data.get('download_start_time', time.time())) if context else 0
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            event_type='DOWNLOAD_COMPLETE',
            function_name='start_youtube_download',
            task_id=task_id,
            details={'time_taken': duration, 'canceled': False},
            action_status={'status': 'success', 'latency': f'{duration}s'}
        )
        logger.info(f'[YT] DOWNLOAD_COMPLETE logged | duration={duration}s')
        # =====================================

        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=final_text,
            parse_mode="HTML"
        )

    except Exception as e:
        logger.exception("A critical exception occurred during the download process:")

        # ====== DOWNLOAD_ERROR LOG ======
        task_id = context.user_data.get('task_id') if context else None
        duration = int(time.time() - context.user_data.get('download_start_time', time.time())) if context else 0
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            event_type='DOWNLOAD_ERROR',
            function_name='start_youtube_download',
            task_id=task_id,
            details={'time_taken': duration, 'error': str(e)},
            action_status={'status': 'error', 'latency': f'{duration}s'}
        )
        logger.info(f'[YT] DOWNLOAD_ERROR logged | duration={duration}s')
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"❌ Error during download: {str(e)}"
        )
    finally:
        context.user_data.pop("active_yt_process", None)
        context.user_data.pop("active_yt_dest_path", None)
        context.user_data.pop("yt_cancelled", None)
