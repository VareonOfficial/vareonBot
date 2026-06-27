import os, asyncio, sqlite3, re
asyncio.set_event_loop(asyncio.new_event_loop())
from datetime import datetime, timezone
from collections import defaultdict
from telegram import Update
from telegram.ext import (Application, CommandHandler, MessageHandler, ConversationHandler, CallbackQueryHandler, 
                          CallbackContext, ContextTypes, filters,
)
from pyrogram import Client as PyroClient
from telethon import TelegramClient as TelethonClient
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.sessions import StringSession
from main.config import pg_conn_auth, get_user_details_from_db, SUPPORT_GROUP_ID, LOGIN_LINK

import config, state

from search_config import search
from search_config.constants import (
    GAMELEECH_PARTS, GAMELEECH_CHOICE, SEARCH_CHOICE, SEARCH_OPTION,
    SEARCH_RANGE, ANIMEFLIX_EPISODE_RANGE, TOONWORLD_QUALITY,
)
from search_config.toonworld4all import handle_toonworld4all_quality, handle_toonworld_quality_choice
from search_config.animeflix import animeflix_episode_range
from search_config.moviesmod import handle_episode_range
from search_config.bollyflix import choose_bollyflix_result
from search_config.gamesleech import handle_gamesleech_zip, handle_gamesleech_parts

from features.music.music import music, music_search_pick_callback
from report.report import report_command, report_buttons
from report.report_history import view_report_details, report_history, delete_report_handler
from report.send_report import handle_report_message, handle_report_subject, finish_report, handle_priority_selection
from report.chat import handle_admin_reply, start_user_reply, finish_user_reply
from features.cancel.cancel import cancel_process
from features.links.links import link_handler, download_button_handler
from features.links.pause_resume import resume_download, pause_download
from features.shared.yt_dlp import youtube_quality_callback
from features.myfiles.myfiles import myfiles, myfiles_callback
from features.myfiles.browse import page_next, page_prev
from features.myfiles.select_actions import (start_rename, handle_rename_input, cancel_generated_link, 
handle_new_folder_input, start_new_folder)
from features.myfiles.move import (start_move_folder, navigate_move_folder, navigate_move_back, move_here, move_page_prev,
move_page_next)
from features.myfiles.utils import handle_reply_name
from features.files.files import handle_file, files
from features.cookies.set_cookies import cookies, cookies_menu, save_cookie
from features.trash.trash import trash, trash_file_detail
from features.trash.trash_handlers import (trash_action_handler, trash_file_action_handler, trash_page_handler,
                                           trash_toggle_handler, trash_select_action_handler, trash_confirm_handler)
from features.trash.purge import start_purge_scheduler
from features.shared.stats import stats_command, close_stats
from features.shared.storage import storage, refresh_storage
from main.state import sessions
from main.utils import cache_file_id, getid_command, _common_menu_handler
from main.config import (
    VAREON_DB, logger, ADMIN_ID, PRIVATE_GROUP_LINK, PYRO_SESSION_TXT,
    RENAME, MOVE_FOLDER, NEW_FOLDER, BOT_TOKEN, USERS_PATH, TELETHON_SESSION_TXT,
)
from vareon_analytics.vr_log import log_wrapper
from vareon_analytics.export_data import handle_export_data
from main.dir_update import (navigate_folder, navigate_back, show_download_folder_menu, handle_download_here_callback,
                             handle_folder_page_navigation)
from infra.broadcast import broadcast_settings, broadcast_command, delete_broadcast, handle_broadcast_message, cancel_broadcast
from infra.settings import settings, handle_toggle_receive_updates, handle_toggle_default_dl
from databases.databases_config import init_db

user_locks = defaultdict(asyncio.Lock)
file_session_lock = asyncio.Lock()
    
def get_logged_in_vareon_id(user_id: int):
    session = sessions.get(user_id)
    if not session:
        return None
    return session.get("vareon_id")

def load_restore_users():
    """Load users from SQLite and return dict (same as old JSON format)."""
    try:
        conn = sqlite3.connect(VAREON_DB)
        cursor = conn.cursor()

        cursor.execute("SELECT telegram_id, username, vareon_id FROM restore_users")
        rows = cursor.fetchall()

        conn.close()

        data = {}
        for telegram_id, username, vareon_id in rows:
            data[str(telegram_id)] = {
                "username": username,
                "vareon_id": vareon_id
            }

        return data

    except Exception as e:
        logger.error(f"Error loading restore_users: {e}")
        return {}

def save_restore_users(data):
    """Save all users to SQLite (replaces JSON dump)."""
    try:
        conn = sqlite3.connect(VAREON_DB)
        cursor = conn.cursor()

        for telegram_id, user_data in data.items():
            try:
                telegram_id = int(telegram_id)
                username = user_data.get("username")
                vareon_id = user_data.get("vareon_id")

                if not vareon_id:
                    continue

                cursor.execute("""
                    INSERT INTO restore_users (telegram_id, username, vareon_id)
                    VALUES (?, ?, ?)
                    ON CONFLICT(telegram_id) DO UPDATE SET
                        username=excluded.username,
                        vareon_id=excluded.vareon_id
                """, (telegram_id, username, vareon_id))

            except Exception as inner_e:
                logger.error(f"Error saving user {telegram_id}: {inner_e}")

        conn.commit()
        conn.close()

    except Exception as e:
        logger.error(f"Error saving restore_users: {e}")

def restore_sessions(application):
    """Restore user sessions from DB."""
    global sessions

    restore_data = load_restore_users()

    for user_id, user_data in restore_data.items():
        try:
            user_id = int(user_id)

            vareon_id = user_data.get("vareon_id")
            username = user_data.get("username")

            if not vareon_id:
                logger.warning(f"Skipping {user_id}: missing vareon_id")
                continue

            sessions[user_id] = {
                "username": username,
                "vareon_id": int(vareon_id)
            }

            logger.info(f"Restored {user_id} → {vareon_id}")

        except Exception as e:
            logger.error(f"Error restoring session for {user_id}: {e}")
################################
# Configuration and Constants
################################
PYRO_SESSION_STRING     = PYRO_SESSION_TXT.read_text().strip()
TELETHON_SESSION_STRING = TELETHON_SESSION_TXT.read_text().strip()

state.pyro_bot_client = PyroClient(
    name=str(config.DATA_DIR / "pyro_bot_session"),
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
)

state.telethon_user_client = TelethonClient(
    session=StringSession(TELETHON_SESSION_STRING),
    api_id=config.API_ID,
    api_hash=config.API_HASH,
)

state.PRIVATE_GROUP_ID = config.PRIVATE_GROUP_ID

################################
# Helper Functions
################################
async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type in ["group", "supergroup"]:
        await update.message.reply_text(
            f"Group ID: `{chat.id}`\n"
            f"Title: {chat.title or '—'}\n",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"This chat ID: `{chat.id}`", parse_mode="Markdown")
        
################################
# Authentication Handlers
################################

async def validate_telegram_token(token: str, telegram_user_id: int):

    try:
        with pg_conn_auth() as conn:
            with conn.cursor() as cur:

                cur.execute("""
                    SELECT 
                        t.vareon_id,
                        u.name,
                        t.expires_at,
                        t.used
                    FROM telegram_login_tokens t
                    JOIN users u ON t.vareon_id = u.vareon_id
                    WHERE t.token = %s
                """, (token,))

                row = cur.fetchone()
                if not row:
                    return None

                vareon_id, name, expires_at, used = row

                # Always generate now in UTC (aware)
                now_utc = datetime.now(timezone.utc)

                # Debug check
                logger.info(f"expires_at={expires_at} | now_utc={now_utc}")

                if used:
                    return None

                if expires_at <= now_utc:
                    return None

                cur.execute("""
                    UPDATE telegram_login_tokens
                    SET used = TRUE
                    WHERE token = %s
                """, (token,))
                conn.commit()

                return {
                    "vareon_id": vareon_id,
                    "name": name
                }

    except Exception as e:
        logger.error(f"Token validation error: {e}")
        return None

@log_wrapper(event_type="COMMAND", function_name="start/login")
async def start(update: Update, context: CallbackContext) -> None:
    user_id = update.message.from_user.id
    first_name = update.message.from_user.first_name
    tg_username = update.message.from_user.username
    first_name = update.message.from_user.first_name or ""
    last_name = update.message.from_user.last_name or ""
    full_name = (first_name + " " + last_name).strip()

    args = context.args
    
    # Check if user already has a session
    existing_session = sessions.get(user_id)
    if existing_session and "vareon_id" in existing_session:
        vareon_id = existing_session["vareon_id"]
        await update.message.reply_text(
            f"✅ You are already logged in with **Vareon ID** : **{vareon_id}** on **Telegram** : **{user_id}**.\n"
            f"If you want to switch accounts, please /logout first and then /login again.",
            parse_mode="Markdown"
        )
        return

    # Special broadcast user
    if user_id == 1074000261:
        if str(user_id) not in broadcast_settings:
            broadcast_settings[str(user_id)] = {"receive_updates": True}
            save_broadcast_settings(broadcast_settings)

    # Deep link login (after web login)
    if args and len(args) > 0:
        token = args[0].strip()
        user_info = await validate_telegram_token(token, user_id)

        if user_info:
            vareon_id = user_info["vareon_id"]
            display_name = user_info["name"]

            # Store session
            session_data = sessions.get(user_id) or {}
            session_data["vareon_id"] = vareon_id
            sessions[user_id] = session_data

            context.application.bot_data.setdefault('user_credentials', {})[user_id] = {
                "username": display_name,
                "vareon_id": vareon_id
            }

            # Persist session
            restore_data = load_restore_users()
            restore_data[str(user_id)] = {
                "username": display_name,
                "vareon_id": vareon_id,
            }
            save_restore_users(restore_data)

            # ── Create user folder ──
            user_folder = f"{USERS_PATH}/{vareon_id:08d}"  
            os.makedirs(user_folder, exist_ok=True)
            logger.info(f"[USER_FOLDER] Created/ensured for vareon_id={vareon_id}: {user_folder}")

            # ── Update telegram_auth ──
            try:
                conn = sqlite3.connect(VAREON_DB)
                cursor = conn.cursor()

                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

                # Ensure username starts with @
                formatted_username = f"@{tg_username}" if tg_username else None

                cursor.execute("""
                    INSERT INTO telegram_auth (
                        vareon_id,
                        telegram_user_id,
                        telegram_username,
                        telegram_full_name,
                        latest_login_at,
                        first_login_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(vareon_id, telegram_user_id) DO UPDATE SET
                        latest_login_at=excluded.latest_login_at,
                        telegram_full_name=excluded.telegram_full_name,
                        telegram_username=excluded.telegram_username
                """, (
                    vareon_id,
                    user_id,
                    formatted_username,
                    full_name,
                    now,
                    now
                ))

                # Add/Update broadcast settings
                cursor.execute("""
                    INSERT INTO broadcast_settings (telegram_user_id, receive_updates)
                    VALUES (?, 1)
                    ON CONFLICT(telegram_user_id) DO NOTHING
                """, (user_id,))
                # Add to live in-memory broadcast list immediately
                if str(user_id) not in broadcast_settings:
                    broadcast_settings[str(user_id)] = {"receive_updates": True}
                    logger.info(f"[BROADCAST] Added new user {user_id} to live broadcast list")

                conn.commit()
                conn.close()

                logger.info(f"[SQLITE AUTH] Updated telegram_auth & broadcast_settings for telegram_user_id={user_id}, vareon_id={vareon_id}")

            except Exception as e:
                logger.error(f"[SQLITE AUTH ERROR] {e}")

            await update.message.reply_text(
                f"🎉 **Login successful!**\n\n"
                f"Welcome back, **{display_name}**\n\n"
                "You can now use:\n"
                "• /link — upload from link\n"
                "• /music — upload music from Spotify and YouTube\n"
                "• /search — search for any movie or show\n"
                "• /myfiles — your files\n"
                "• /storage — storage info\n"
                "• /logout — sign out",
                parse_mode="Markdown"
            )
            return

        else:
            await update.message.reply_text("❌ Invalid or expired link.\nUse /login to get a new one.")
            return

    await update.message.reply_text(
        f"👋 Hi **{first_name}**! Welcome to Vareon bot!\n\n"
        f"Log in with your Vareon account to use the bot:\n\n"
        f"➜ [Login / Create Account]({LOGIN_LINK})\n\n"
        f"You'll be brought back here automatically after login.\n\n"
        f"Your Telegram User ID: {update.effective_user.id}",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

async def login(update: Update, context: CallbackContext) -> None:
    await start(update, context)
    
async def logout(update: Update, context: CallbackContext) -> int:
    user_id = update.message.from_user.id

    if user_id not in sessions:
        await update.message.reply_text("❌ You are not logged in. Use /login first.")
        return ConversationHandler.END

    vareon_id = sessions[user_id].get("vareon_id")

    # 🔹 Remove active session
    sessions.pop(user_id, None)

    # 🔹 Remove from SQLite (restore_users + telegram_auth)
    try:
        conn = sqlite3.connect(VAREON_DB)
        cursor = conn.cursor()

        # Remove from restore_users
        cursor.execute("""
            DELETE FROM restore_users
            WHERE telegram_id = ?
        """, (user_id,))

        # 🔥 Remove from telegram_auth
        if vareon_id:
            cursor.execute("""
                DELETE FROM telegram_auth
                WHERE vareon_id = ? AND telegram_user_id = ?
            """, (vareon_id, user_id))

        # 🔥 Remove from broadcast_settings
        cursor.execute("""
            DELETE FROM broadcast_settings
            WHERE telegram_user_id = ?
        """, (user_id,))
        # Remove from live in-memory list
        broadcast_settings.pop(str(user_id), None)
        logger.info(f"[BROADCAST] Removed user {user_id} from live broadcast list")

        conn.commit()
        conn.close()

        logger.info(f"[LOGOUT] Removed DB rows for telegram_id={user_id}, vareon_id={vareon_id}")

    except Exception as e:
        logger.error(f"[LOGOUT ERROR] DB delete failed: {e}")

    await update.message.reply_text(
        "✅ Successfully logged out.\n"
        "👉 Use /login to sign in again."
    )

    return ConversationHandler.END

# Function to save broadcast settings
def save_broadcast_settings(settings):
    """Save the broadcast settings to a file or database (implementation needed)."""
    pass  # Implement actual saving mechanism

################################
# Help function
################################

async def help_command(update: Update, context: CallbackContext) -> None:
    help_text = """
✦ *VAREON Bot — Command Reference*

━━━━━━━━━━━━━━━━
👤 *Account*
━━━━━━━━━━━━━━━━
`/login` — Redirects you to vareon\.top for web auth\. Log in with Google or your manual account — the bot links automatically after token verification\.
`/logout` — Ends your session\. Most features are disabled until you log in again\.
`/account` — View your current account details and session info\.
`/settings` — Set a default download folder and toggle bot update notifications\.

━━━━━━━━━━━━━━━━
⬇️ *Downloads*
━━━━━━━━━━━━━━━━
`/link` — Paste a direct URL, YouTube video, short, or playlist to download it straight to your storage at high speed\.
`/music` — Download tracks from Spotify or YouTube\. Choose to save to your directory or send the file back to Telegram\.
`/cookies` — Set your Spotify or YouTube cookies to unlock premium downloads\. Your cookies are handled securely — never share them with anyone outside this bot\.

━━━━━━━━━━━━━━━━
📁 *Files & Storage*
━━━━━━━━━━━━━━━━
`/myfiles` — Browse your folders and files\. Select multiple items to move, compress, or delete them\. Generate high\-speed download links for any file or folder\.
`/storage` — See how much storage you've used and a breakdown of what's taking up space\.

━━━━━━━━━━━━━━━━
🔍 *Search*
━━━━━━━━━━━━━━━━
`/search` — Search for movies, apps, software, and games instantly — no need to visit external sites\.

━━━━━━━━━━━━━━━━
🛠 *Support & Control*
━━━━━━━━━━━━━━━━
`/report` — Report a bug with subject, priority, and details\. Also opens a direct line to the developer — you can view report history or request contact from here\.
`/cancel` — Stop any active download or ongoing process and reset the bot to a clean state\.

━━━━━━━━━━━━━━━━
📌 *Note:* You must be logged in to use most features\. Use `/login` to get started\.
"""
    await update.message.reply_text(help_text, parse_mode="MarkdownV2")
    
################################
# Account setup
################################

async def accounts(update: Update, context: CallbackContext):
    user = update.message.from_user
    user_id = user.id
    full_name = f"{user.first_name} {user.last_name}".strip() if user.last_name else user.first_name

    vareon_id = get_logged_in_vareon_id(user_id)
    if not vareon_id:
        await update.message.reply_text("❌ You are not logged in. Use /login first.")
        return ConversationHandler.END

    user_data = get_user_details_from_db(vareon_id)

    if not user_data:
        await update.message.reply_text("❌ Account not found. Please contact support.")
        return ConversationHandler.END

    text = (
    f"User Details -\n"
    f"👤 Telegram ID: {user_id}\n"
    f"📛 Telegram name: {full_name}\n\n"

    f"Account Details -\n"
    f"Name: {user_data['name']}\n"
    f"Vareon ID: {vareon_id}\n"
    f"Username: {user_data['vareon_username']}\n"
    f"Email: {user_data['email']}\n\n"
    f"Language: {user_data['language']}\n"
    f"Country: {user_data['country']}\n"
    f"ZIP: {user_data['zip']}\n"
    f"Location: {user_data['location']}\n\n"

    f"Membership: Free user\n"
    f"Billing: 0$, free limit\n\n"
    )

    await update.message.reply_text(text)

################################
# Application Setup
################################
async def debug_all(update: Update, context: CallbackContext):
    logger.info(f"[DEBUG_ALL] update={update}")
def setup_handlers(application: Application) -> None:
    """
    Register all handlers for the application.
    ⚠️  ORDER IS CRITICAL — handlers are matched top-to-bottom within each group.
         Moving a handler can silently break another feature.
         Read the priority notes before touching anything here.
    """
    # ─────────────────────────────────────────────────────────────────────────────
    # 0.  STARTUP & GLOBAL HOOKS
    # ─────────────────────────────────────────────────────────────────────────────
    restore_sessions(application)
    
    # group -99: fires first on every callback update — never remove or reorder
    application.add_handler(CallbackQueryHandler(debug_all), group=-99)
    application.add_error_handler(error_handler)

    # ─────────────────────────────────────────────────────────────────────────────
    # 1.  CANCEL
    # ─────────────────────────────────────────────────────────────────────────────
    application.add_handler(CallbackQueryHandler(cancel_process, pattern=r"^cancel_process($|\|)"))
    application.add_handler(CommandHandler("cancel", cancel_process))

    # ─────────────────────────────────────────────────────────────────────────────
    # 2.  SEARCH  (/search → text input → results → episode range, etc.)
    # ─────────────────────────────────────────────────────────────────────────────
    search_handler = ConversationHandler(
        entry_points=[
            CommandHandler("search", search.start_search),
        ],
        states={
            SEARCH_CHOICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, search.choose_result),
            ],
            SEARCH_OPTION: [
                CallbackQueryHandler(search.select_option,          pattern=r"^opt_"),
                CallbackQueryHandler(handle_toonworld4all_quality,  pattern=r"^tw_opt_\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, choose_bollyflix_result),
            ],
            TOONWORLD_QUALITY: [
                CallbackQueryHandler(handle_toonworld_quality_choice, pattern=r"^tw_quality_\d+$"),
            ],
            SEARCH_RANGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_episode_range),
            ],
            ANIMEFLIX_EPISODE_RANGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, animeflix_episode_range),
            ],
            GAMELEECH_CHOICE: [
                CallbackQueryHandler(handle_gamesleech_zip, pattern=r"^GAMELEECH_ZIP$"),
            ],
            GAMELEECH_PARTS: [
                # ZIP button must remain reachable even while waiting for part text input
                CallbackQueryHandler(handle_gamesleech_zip,   pattern=r"^GAMELEECH_ZIP$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_gamesleech_parts),
            ],
        },
        fallbacks=[],
        allow_reentry=True,)
    application.add_handler(search_handler)

    # ─────────────────────────────────────────────────────────────────────────────
    # 3.  MYFILES  — file manager (browse, rename, move, new folder)
    # ─────────────────────────────────────────────────────────────────────────────
    # 3a. New-folder conversation
    new_folder_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_new_folder, pattern="^new_folder$"),
        ],
        states={
            NEW_FOLDER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_new_folder_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_process)],
        allow_reentry=True,)
    application.add_handler(new_folder_conv)
    # 3b. Rename conversation (file or folder)
    rename_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_rename, pattern=r"^(rename|rename_folder)\|"),
        ],
        states={
            RENAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_rename_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_process)],
        allow_reentry=True,)
    application.add_handler(rename_conv)
    # 3c. Move-folder conversation
    move_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_move_folder, pattern=r"^move_folder\|"),
        ],
        states={
            MOVE_FOLDER: [
                CallbackQueryHandler(navigate_move_folder, pattern=r"^navigate_move\|"),
                CallbackQueryHandler(navigate_move_back,   pattern="^navigate_move_back$"),
                CallbackQueryHandler(move_here,            pattern="^move_here$"),
            ],
        },
        fallbacks=[
            CommandHandler("start",  start),
            CommandHandler("logout", logout),
            CommandHandler("help",   help_command),
        ],
        allow_reentry=True,)
    application.add_handler(move_conv)
    # 3d. /myfiles command
    application.add_handler(CommandHandler("myfiles", myfiles))
    # 3e. Core file-manager button actions
    application.add_handler(CallbackQueryHandler(myfiles_callback,
        pattern=(
            r"^(open|file|get_link|upload_file|move_file|compress|extract|delete|refresh$|back|move_nav|extract_nav"
            r"|move_execute|extract_execute|extract_to_folder|compress_format|multi_exec|multi_extract|cancel_compress"
            r"|multi_select|select\|.*|select_all|multi_delete|multi_move|multi_compress|multi_cancel)"
        ),))
    # 3f. Dynamic extract / move navigation (also inside conversations above)
    application.add_handler(CallbackQueryHandler(myfiles_callback,
        pattern=r"^(extract_nav\|.*|extract_nav_back|extract_execute|move_nav\|.*|move_nav_back|move_execute)$",))
    application.add_handler(CallbackQueryHandler(navigate_move_folder, pattern=r"^navigate_move\|"))
    application.add_handler(CallbackQueryHandler(navigate_move_back,   pattern="^navigate_move_back$"))
    application.add_handler(CallbackQueryHandler(move_here,            pattern="^move_here$"))
    # 3g. Pagination (browse pages inside myfiles)
    application.add_handler(CallbackQueryHandler(page_prev,      pattern=r"^page_prev\|"))
    application.add_handler(CallbackQueryHandler(page_next,      pattern=r"^page_next\|"))
    application.add_handler(CallbackQueryHandler(move_page_prev, pattern=r"^move_page_prev\|"))
    application.add_handler(CallbackQueryHandler(move_page_next, pattern=r"^move_page_next\|"))
    # 3h. Folder / directory navigation (dir_update.py — used while choosing download location)
    application.add_handler(CallbackQueryHandler(handle_folder_page_navigation, pattern="^folder_page\\|"))
    application.add_handler(CallbackQueryHandler(navigate_back,                 pattern="^navigate_back$"))
    application.add_handler(CallbackQueryHandler(navigate_folder,               pattern="^navigate\\|.*$"))
    application.add_handler(CallbackQueryHandler(show_download_folder_menu,     pattern="^open_download_menu$"))
    application.add_handler(CallbackQueryHandler(handle_download_here_callback, pattern="^download_here"))
    # 3i. Inline-reply naming (used by compress / extract single-folder flow)
    application.add_handler(MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND, handle_reply_name))
    # 3j. Generated link cancel button
    application.add_handler(CallbackQueryHandler(cancel_generated_link, pattern=r"^cancel_generated_link:"))
    
    # ─────────────────────────────────────────────────────────────────────────────
    # 4.  FILES
    # ─────────────────────────────────────────────────────────────────────────────
    application.add_handler(CommandHandler("files", files))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE
            & (filters.Document.ALL| filters.VIDEO| filters.AUDIO| filters.PHOTO| filters.VOICE
               | filters.VIDEO_NOTE)& ~filters.Regex(r"^/"),
            handle_file,
        ),group=-5,)
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE
            & (filters.VIDEO | filters.Document.ALL | filters.PHOTO),
            cache_file_id,
        ),group=-1,)

    # ─────────────────────────────────────────────────────────────────────────────
    # 5.  TRASH
    # ─────────────────────────────────────────────────────────────────────────────
    application.add_handler(CommandHandler("trash", trash))
    application.add_handler(CallbackQueryHandler(trash_confirm_handler,       pattern=r"^trash_confirm:"))
    application.add_handler(CallbackQueryHandler(trash_page_handler,          pattern=r"^trash_page:"))
    application.add_handler(CallbackQueryHandler(trash_toggle_handler,        pattern=r"^trash_toggle:"))
    application.add_handler(CallbackQueryHandler(trash_select_action_handler, pattern=r"^trash_select_action:"))
    application.add_handler(CallbackQueryHandler(trash_file_detail,           pattern=r"^trash_file:\d+$"))
    application.add_handler(CallbackQueryHandler(trash_action_handler,        pattern=r"^trash_action:"))
    application.add_handler(CallbackQueryHandler(trash_file_action_handler,   pattern=r"^trash_file_action:"))

    # ─────────────────────────────────────────────────────────────────────────────
    # 6.  LINK — URL downloads, pause, resume
    # ─────────────────────────────────────────────────────────────────────────────
    application.add_handler(CommandHandler("link", link_handler))
    application.add_handler(CallbackQueryHandler(download_button_handler, pattern="^start_download$"))
    application.add_handler(CallbackQueryHandler(pause_download,          pattern="^pause_download$"))
    application.add_handler(CallbackQueryHandler(resume_download,         pattern="^resume_download$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, link_handler))

    # ─────────────────────────────────────────────────────────────────────────────
    # 7.  MUSIC
    # ─────────────────────────────────────────────────────────────────────────────
    application.add_handler(CommandHandler("music", music))
    application.add_handler(CallbackQueryHandler(music_search_pick_callback, pattern=r"^music_search_pick:"))

    # ─────────────────────────────────────────────────────────────────────────────
    # 8.  COOKIES
    # ─────────────────────────────────────────────────────────────────────────────
    application.add_handler(CommandHandler("cookies", cookies))
    application.add_handler(CallbackQueryHandler(cookies_menu, pattern=r"^cookie_options"))
    application.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, save_cookie),group=-2,)

    # ─────────────────────────────────────────────────────────────────────────────
    # 9.  STORAGE
    # ─────────────────────────────────────────────────────────────────────────────
    application.add_handler(CommandHandler("storage", storage))
    application.add_handler(CallbackQueryHandler(refresh_storage, pattern="^refresh_storage$"))

    # ─────────────────────────────────────────────────────────────────────────────
    # 10. REPORT SYSTEM  — bug reports and user↔admin support chat
    # ─────────────────────────────────────────────────────────────────────────────
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_report_subject), group=-3,)
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_report_message), group=-2,)
    application.add_handler(MessageHandler(filters.Chat(int(SUPPORT_GROUP_ID)) & filters.REPLY, handle_admin_reply,), group=-4,)
    application.add_handler(CallbackQueryHandler(finish_report,             pattern="^finish_report$"))
    application.add_handler(CallbackQueryHandler(handle_priority_selection, pattern="^priority_"))
    application.add_handler(CallbackQueryHandler(view_report_details,       pattern=r"^view_rep:"))
    application.add_handler(CallbackQueryHandler(delete_report_handler,     pattern=r"^rep_delete:"))
    application.add_handler(CallbackQueryHandler(report_history,            pattern=r"^report_history"))
    application.add_handler(CallbackQueryHandler(report_buttons,            pattern=r"^report_"))
    # User-side reply flow (user replies to admin from inside the bot)
    application.add_handler(CallbackQueryHandler(start_user_reply,  pattern="^start_reply:"))
    application.add_handler(CallbackQueryHandler(finish_user_reply, pattern="^finish_user_reply$"))

    # ─────────────────────────────────────────────────────────────────────────────
    # 11. SETTINGS
    # ─────────────────────────────────────────────────────────────────────────────
    application.add_handler(CommandHandler("settings", settings))
    application.add_handler(CallbackQueryHandler(handle_toggle_default_dl,      pattern="^toggle_default_dl$"))
    application.add_handler(CallbackQueryHandler(handle_toggle_receive_updates, pattern="^toggle_receive_updates$"))

    # ─────────────────────────────────────────────────────────────────────────────
    # 12. ACCOUNT / AUTH COMMANDS
    # ─────────────────────────────────────────────────────────────────────────────
    application.add_handler(CommandHandler("start",   start))
    application.add_handler(CommandHandler("login",   start))
    application.add_handler(CommandHandler("logout",  logout))
    application.add_handler(CommandHandler("account", accounts))
    application.add_handler(CommandHandler("help",    help_command))
    application.add_handler(CommandHandler("id",      get_chat_id))
    application.add_handler(CommandHandler("export_data", handle_export_data))

    # ─────────────────────────────────────────────────────────────────────────────
    # 13. ADMIN PANEL
    # ─────────────────────────────────────────────────────────────────────────────
    application.add_handler(CommandHandler("getid",           getid_command))
    application.add_handler(CommandHandler("stats",           stats_command))
    application.add_handler(CommandHandler("broadcast",       broadcast_command))
    application.add_handler(CommandHandler("deletebroadcast", delete_broadcast))

    application.add_handler(MessageHandler(filters.ALL & filters.User(user_id=ADMIN_ID),handle_broadcast_message,))
    application.add_handler(CallbackQueryHandler(_common_menu_handler, pattern="^_common_menu:"))
    application.add_handler(CallbackQueryHandler(close_stats,          pattern="close_stats"))
    application.add_handler(CallbackQueryHandler(cancel_broadcast,     pattern="^cancel_broadcast$"))
    application.add_handler(CallbackQueryHandler(youtube_quality_callback, pattern="^yt_quality\\|.*$"))
    
################################
# Error Handler
################################

async def error_handler(update: Update, context: CallbackContext) -> None:
    """Log errors caused by updates."""
    if update and update.channel_post:
        return
    logger.error('Update "%s" caused error "%s"', update, context.error)
    if update.callback_query:
        await update.callback_query.answer("An error occurred. Please try again.")
    elif update.message:
        await update.message.reply_text("❌ An error occurred. Please try again.")
        
async def heartbeat():
    while True:
        logger.info("Bot heartbeat alive")
        await asyncio.sleep(60)

################################
# Main Function
################################
bot_id = None
application = None

async def run_all():
    global bot_id, application

    # ── Pyrogram bot client ──────────────────────────────────────────
    logger.info("Starting Pyrogram bot client...")
    await state.pyro_bot_client.start()
    me = await state.pyro_bot_client.get_me()
    bot_id = me.id
    logger.info(f"Pyro bot client started. ID: {bot_id}")

    # ── Telethon user client ─────────────────────────────────────────
    logger.info("Starting Telethon user client...")
    await state.telethon_user_client.start()
    try:
        await state.telethon_user_client.get_entity(PRIVATE_GROUP_LINK)
        logger.info("✅ Private group peer cached via Telethon")
    except Exception:
        try:
            await state.telethon_user_client(
                JoinChannelRequest(PRIVATE_GROUP_LINK)
            )
            logger.info("✅ Private group joined via Telethon")
        except Exception as e2:
            logger.error(f"Failed to cache private group peer: {e2}")

    # ── Start purge scheduler ───────────────────────────────────────
    if os.getenv("ENABLE_PURGE", "true").lower() == "true":
        start_purge_scheduler()
    # ── PTB Application ──────────────────────────────────────────────
    logger.info("Starting PTB application...")
    application = Application.builder().token(BOT_TOKEN).build()
    state.application = application

    logger.info("Creating and Initialising databases...")
    init_db()
    setup_handlers(application)

    await application.initialize()
    await application.start()
    application.create_task(heartbeat())
    await application.updater.start_polling()
    logger.info("PTB application started.")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run_all())