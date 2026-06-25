import asyncio
import time
import os
import datetime
import uuid

from telegram import Update
from telegram.ext import ContextTypes
from urllib.parse import urlparse

from main.state import sessions, report_mode, running_tasks
from main.config import logger, USERS_PATH, COOKIES_PATH
from main.dir_update import show_download_folder_menu
from features.music.music_search import search_and_show
from features.music.utils import build_cancel_keyboard, validate_youtube_link
from features.music.yt_helpers.yt_downloader import scrape_youtube_to_download
from features.music.sp_helpers.sp_downloader import scrape_spotify_to_youtube
from features.music.telegram_button import show_upload_option


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — Cookie helpers (module-level so all functions can use them)
# ─────────────────────────────────────────────────────────────────────────────

def parse_netscape_cookies(path: str) -> list[dict]:
    """Parse a Netscape-format cookie file into a list of cookie dicts."""
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


def get_cookie_expiry(cookies: list[dict], cookie_name: str, domain_filter: str = None):
    """
    Find a cookie by name (and optional domain) and return (timestamp, date_str).
    Returns (None, None) if not found or expiry is unreadable.
    """
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

    ts       = int(float(expiry_raw))
    date_str = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%a, %d %b %Y")
    return ts, date_str


def get_sp_dc(vareon_id: str) -> str | None:
    """
    Read the user's Spotify cookie file and return sp_dc value if valid and not expired.
    Returns None if file missing, cookie missing, or expired.
    """
    path = COOKIES_PATH / "spotify" / f"{vareon_id}.txt"
    if not os.path.exists(path):
        return None

    cookies   = parse_netscape_cookies(path)
    sp_cookie = next((c for c in cookies if c["name"] == "sp_dc"), None)
    if not sp_cookie:
        return None

    ts_raw = sp_cookie["expiry"].replace("~", "").replace("≈", "").strip()
    if not ts_raw.replace(".", "", 1).isdigit():
        return None

    if int(time.time()) >= int(float(ts_raw)):
        return None  # expired

    return sp_cookie["value"]


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — /music command handler
# ─────────────────────────────────────────────────────────────────────────────

async def music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id      = update.message.from_user.id
    session_data = sessions.get(user_id)

    if not session_data:
        await update.message.reply_text("❌ Please login first using /login.")
        return

    if report_mode.get(user_id, False):
        return

    vareon_id = session_data.get("vareon_id")

    # ── No args: show cookie status info message ──────────────────────────────
    if len(context.args) == 0:
        spotify_cookie_path = COOKIES_PATH / "spotify" / f"{vareon_id}.txt"
        youtube_cookie_path = COOKIES_PATH / "youtube" / f"{vareon_id}.txt"

        spotify_expiry_text = "🔴 Spotify — Not connected"
        youtube_expiry_text = "🔴 YouTube — Not connected"

        # Spotify
        if os.path.exists(spotify_cookie_path):
            cookies = parse_netscape_cookies(spotify_cookie_path)
            if not cookies:
                spotify_expiry_text = "❌ Spotify (invalid cookie file)"
            else:
                ts, date_str = get_cookie_expiry(cookies, "sp_dc")
                if ts is None:
                    spotify_expiry_text = "⚠️ Spotify (sp_dc missing or expiry unknown)"
                    logger.warning("[SPOTIFY CHECK] sp_dc not found or expiry unreadable")
                elif int(time.time()) > ts:
                    spotify_expiry_text = "⚠️ Spotify — Expired"
                else:
                    spotify_expiry_text = f"🟢 Spotify — Expires on {date_str} UTC"
        else:
            logger.warning("[SPOTIFY CHECK] Cookie file does NOT exist")

        # YouTube
        if os.path.exists(youtube_cookie_path):
            cookies = parse_netscape_cookies(youtube_cookie_path)
            if not cookies:
                youtube_expiry_text = "❌ YouTube (invalid cookie file)"
            else:
                ts, date_str = get_cookie_expiry(cookies, "SID", domain_filter="youtube.com")
                if ts is None:
                    youtube_expiry_text = "⚠️ YouTube (SID missing or expiry unknown)"
                    logger.warning("[YOUTUBE CHECK] SID not found or expiry unreadable")
                elif int(time.time()) > ts:
                    youtube_expiry_text = "⚠️ YouTube — Expired"
                else:
                    youtube_expiry_text = f"🟢 YouTube — Expires on {date_str} UTC"
        else:
            logger.warning("[YOUTUBE CHECK] Cookie file does NOT exist")

        await update.message.reply_text(
            "🎵 *Music Downloader*\n\n"
            "Use the `/music` command to download songs from supported platforms.\n\n"
            "📌 *How to use:*\n"
            "`/music <link or song name>`\n\n"
            "📎 *Examples:*\n"
            "`/music https://music.youtube.com/...`\n"
            "`/music push the feeling on`\n\n"
            "🔎 *Supported Services:*\n"
            f"{spotify_expiry_text}\n"
            f"{youtube_expiry_text}\n\n"
            "🔐 *Private / Restricted Content:*\n"
            "Some tracks require account access. Use /cookies to connect your account "
            "and enable downloading private or region-locked content.",
            parse_mode="Markdown"
        )
        return

    raw       = " ".join(context.args).strip()
    user_root = os.path.abspath(f"{USERS_PATH}/{vareon_id}")

    # ── Text search: user typed a song name instead of a link ────────────────
    parsed = urlparse(raw)
    is_url = parsed.scheme in ("http", "https") and bool(parsed.netloc)

    if not is_url:
        sp_dc = get_sp_dc(vareon_id)
        await search_and_show(raw, update, context, vareon_id, sp_dc)
        return

    # ── URL: set up pending_music and show folder/upload menus ───────────────
    link = raw

    context.user_data["pending_music"] = {
        "link":      link,
        "vareon_id": vareon_id,
        "task_id":   str(uuid.uuid4()),
    }
    context.user_data["path_stack"]   = [user_root]
    context.user_data["path_map"]     = {}
    context.user_data["current_mode"] = "music_download_select"

    msg1 = await show_download_folder_menu(update, context)
    msg2 = await show_upload_option(context, update.effective_chat.id, link, vareon_id)

    context.user_data["music_ui_messages"] = []
    if msg1:
        context.user_data["music_ui_messages"].append(msg1.message_id)
    if msg2:
        context.user_data["music_ui_messages"].append(msg2.message_id)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — Callback: user picks YouTube or Spotify from search results
# ─────────────────────────────────────────────────────────────────────────────

async def music_search_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Fires when user clicks a search result button (YouTube or Spotify).
    Deletes the result message and resumes the normal /music <link> flow.

    Register in bot.py:
        application.add_handler(CallbackQueryHandler(
            music_search_pick_callback, pattern=r"^music_search_pick:"
        ))
    """
    query = update.callback_query
    await query.answer()
    
    # callback_data format: "music_search_pick:yt" or "music_search_pick:sp"
    _, source = query.data.split(":", 1)
    links = context.user_data.pop("music_search_links", {})
    link  = links.get(source)

    if not link:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="❌ Search result expired. Please search again."
        )
        return

    session_data = sessions.get(query.from_user.id)
    if not session_data:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="❌ Session expired. Please /login again."
        )
        return

    vareon_id = session_data.get("vareon_id")
    user_root = os.path.abspath(f"{USERS_PATH}/{vareon_id}")

    context.user_data["pending_music"] = {
        "link":      link,
        "vareon_id": vareon_id,
        "task_id":   str(uuid.uuid4()),
    }
    context.user_data["path_stack"]   = [user_root]
    context.user_data["path_map"]     = {}
    context.user_data["current_mode"] = "music_download_select"

    msg1 = await show_download_folder_menu(update, context)
    msg2 = await show_upload_option(context, query.message.chat_id, link, vareon_id)

    context.user_data["music_ui_messages"] = []
    if msg1:
        context.user_data["music_ui_messages"].append(msg1.message_id)
    if msg2:
        context.user_data["music_ui_messages"].append(msg2.message_id)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — Start the actual download (called from dir_update / folder pick)
# ─────────────────────────────────────────────────────────────────────────────

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