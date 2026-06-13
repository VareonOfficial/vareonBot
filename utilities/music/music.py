import asyncio
import time
import os
import datetime
import uuid 
from telegram import Update
from telegram.ext import ContextTypes
from main.state import sessions, report_mode,running_tasks
from pathlib import Path
from main.config import logger, USERS_PATH, COOKIES_PATH
from main.dir_update import show_download_folder_menu
from utilities.music.utils import build_cancel_keyboard, validate_youtube_link
from utilities.music.yt_helpers.yt_downloader import scrape_youtube_to_download
from utilities.music.sp_helpers.sp_downloader import scrape_spotify_to_youtube
from utilities.music.telegram_button import show_upload_option

################################
# 🎵 MAIN MUSIC COMMAND
################################

async def music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    session_data = sessions.get(user_id)

    if not session_data:
        await update.message.reply_text("❌ Please login first using /login.")
        return

    if report_mode.get(user_id, False):
        return

    if len(context.args) == 0:
        vareon_id = session_data.get("vareon_id")

        spotify_cookie_path = COOKIES_PATH / "spotify" / f"{vareon_id}.txt"
        youtube_cookie_path = COOKIES_PATH / "youtube" / f"{vareon_id}.txt"

        spotify_expiry_text = "🔴 Spotify — Not connected"
        youtube_expiry_text = "🔴 YouTube — Not connected"

        def parse_netscape_cookies(path):
            """Parse a NETSCAPE cookie file into list of dicts with name, domain, expiry, value."""
            cookies = []
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = line.split("\t")
                        if len(parts) < 7:
                            parts = line.split()
                        if len(parts) >= 7:
                            cookies.append({
                                "domain": parts[0],
                                "path":   parts[2],
                                "secure": parts[3],
                                "expiry": parts[4],
                                "name":   parts[5],
                                "value":  parts[6],
                            })
            except Exception as e:
                logger.error("[COOKIE PARSE] Failed: %s", e)
            return cookies

        def get_expiry_text(cookies, cookie_name, domain_filter=None):
            """Returns (expiry_timestamp, expiry_date_str) or (None, None) if not found."""
            cookie = next(
                (c for c in cookies
                 if c["name"] == cookie_name
                 and (domain_filter is None or domain_filter in c["domain"])),
                None
            )
            if not cookie:
                return None, None
            expiry_raw = cookie["expiry"].replace("~", "").replace("≈", "").strip()
            if not expiry_raw.replace(".", "", 1).isdigit():
                return None, None
            ts = int(float(expiry_raw))
            date_str = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%a, %d %b %Y")
            return ts, date_str

        # ── SPOTIFY ───────────────────────────────────────────
        if os.path.exists(spotify_cookie_path):
            cookies = parse_netscape_cookies(spotify_cookie_path)
            if not cookies:
                spotify_expiry_text = "❌ Spotify (invalid cookie file)"
            else:
                ts, date_str = get_expiry_text(cookies, "sp_dc")
                if ts is None:
                    spotify_expiry_text = "⚠️ Spotify (sp_dc missing or expiry unknown)"
                    logger.warning("[SPOTIFY CHECK] sp_dc not found or expiry unreadable")
                elif int(time.time()) > ts:
                    spotify_expiry_text = "⚠️ Spotify — Expired"
                else:
                    spotify_expiry_text = f"🟢 Spotify — Expires on {date_str} UTC"
        else:
            logger.warning("[SPOTIFY CHECK] Cookie file does NOT exist")

        # ── YOUTUBE ───────────────────────────────────────────
        if os.path.exists(youtube_cookie_path):
            cookies = parse_netscape_cookies(youtube_cookie_path)
            if not cookies:
                youtube_expiry_text = "❌ YouTube (invalid cookie file)"
            else:
                ts, date_str = get_expiry_text(cookies, "SID", domain_filter="youtube.com")
                if ts is None:
                    youtube_expiry_text = "⚠️ YouTube (SID missing or expiry unknown)"
                    logger.warning("[YOUTUBE CHECK] SID not found or expiry unreadable")
                elif int(time.time()) > ts:
                    youtube_expiry_text = f"⚠️ YouTube — Expired"
                else:
                    youtube_expiry_text = f"🟢 YouTube — Expires on {date_str} UTC"
        else:
            logger.warning("[YOUTUBE CHECK] Cookie file does NOT exist")

        await update.message.reply_text(
            "🎵 *Music Downloader*\n\n"
            "Use the `/music` command to download songs from supported platforms.\n\n"
            "📌 *How to use:*\n"
            "`/music <link>`\n\n"
            "📎 *Example:*\n"
            "`/music https://music.youtube.com/...`\n\n"
            "🔎 *Supported Services:*\n"
            f"{spotify_expiry_text}\n"
            f"{youtube_expiry_text}\n\n"
            "🔐 *Private / Restricted Content:*\n"
            "Some tracks require account access. Use /cookies to connect your account "
            "and enable downloading private or region-locked content.",
            parse_mode="Markdown"
        )
        return

    link = context.args[0].strip()
    vareon_id = session_data.get("vareon_id")

    user_root = os.path.abspath(f"{USERS_PATH}/{vareon_id}")

    context.user_data["pending_music"] = {
        "link": link,
        "vareon_id": vareon_id,
        "task_id": str(uuid.uuid4())
    }

    context.user_data["path_stack"] = [user_root]
    context.user_data["path_map"] = {}
    context.user_data["current_mode"] = "music_download_select"

    msg1 = await show_download_folder_menu(update, context)
    msg2 = await show_upload_option(context, update.effective_chat.id, link, vareon_id)

    context.user_data["music_ui_messages"] = []
    if msg1:
        context.user_data["music_ui_messages"].append(msg1.message_id)
    if msg2:
        context.user_data["music_ui_messages"].append(msg2.message_id)
        
async def start_music_download(
    link: str,
    download_path: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    vareon_id: str,
    user_id: int,
    task_id: str,
    target: str = None
):
    chat_id = update.effective_chat.id
    msg_ids = context.user_data.pop("music_ui_messages", [])

    for msg_id in msg_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except:
            pass
        
    msg = update.effective_message
    progress_msg = await msg.reply_text(
    f"🔍 Starting extraction: {link}",
    reply_markup=build_cancel_keyboard(task_id),
    link_preview_options={"is_disabled": True}
    )   
    running_tasks[task_id] = {
        "task": None,
        "message": progress_msg,
        "user_id": user_id,
        "process": None,
        "cancelling": False,
        "keyboard": build_cancel_keyboard(task_id),
    }
    
    if "open.spotify.com" in link:
        async def spotify_task():
            try:
                final_links = await scrape_spotify_to_youtube(
                    link,
                    download_path,
                    progress_msg,
                    vareon_id,
                    task_id,
                    update=update,
                    context=context,
                    target=target,
                    running_tasks=running_tasks,
                )
                if not final_links:
                    await progress_msg.edit_text("❌ No tracks found.")
                    return
                
                await progress_msg.edit_text("✅ Task completed.", reply_markup=None)

            except Exception as e:
                await progress_msg.edit_text(f"❌ Error:\n{e}")

            finally:
                running_tasks.pop(task_id, None)

        task = asyncio.create_task(spotify_task())

    # 🎬 YOUTUBE FLOW
    else:
        is_valid = await validate_youtube_link(link, progress_msg)
        if not is_valid:
            return
        async def youtube_task():
            try:
                final_links = await scrape_youtube_to_download(
                    link,
                    download_path,
                    progress_msg,
                    vareon_id,
                    task_id,
                    update=update,
                    context=context,
                    target=target,
                    running_tasks=running_tasks,
                )
                if not final_links:
                    await progress_msg.edit_text("❌ No tracks found.")
                    return

                await progress_msg.edit_text("✅ Task completed.", reply_markup=None)

            except Exception as e:
                await progress_msg.edit_text(f"❌ Error:\n{e}")

            finally:
                running_tasks.pop(task_id, None)
        task = asyncio.create_task(youtube_task())
    running_tasks[task_id]["task"] = task    