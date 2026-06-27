from main.utils import format_size, rate_limit_interaction, edit_message_if_changed
import os, time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ConversationHandler, CallbackContext
from main.state import sessions, report_mode
from main.config import logger, USERS_PATH

################################
# Constants
################################
STORAGE_QUOTA_BYTES = 100 * 1024 ** 3  # 100 GB fixed quota — change only here later
STORAGE_QUOTA_LABEL = "100 GB"
BAR_FILLED = "🟩"
BAR_EMPTY  = "⬜"
BAR_BLOCKS = 10

################################
# Utility Functions
################################

def smart_format(size_bytes):
    """Auto-scale bytes → KB / MB / GB / TB."""
    for unit, factor in [("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)]:
        if size_bytes >= factor:
            return f"{size_bytes / factor:.2f} {unit}"
    return f"{size_bytes} B"


def get_folder_size(path):
    total = 0.0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                if os.path.isfile(fp):
                    total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def count_files(path):
    total = 0
    for _, _, filenames in os.walk(path):
        total += len(filenames)
    return total


def build_bar(percentage):
    filled = round(percentage / 100 * BAR_BLOCKS)
    filled = max(0, min(BAR_BLOCKS, filled))
    return BAR_FILLED * filled + BAR_EMPTY * (BAR_BLOCKS - filled)


def get_status(percentage):
    if percentage >= 99:
        return "🔴 Full"
    elif percentage >= 90:
        return "🟠 Warning"
    elif percentage >= 80:
        return "🟡 Optimized"
    else:
        return "🟢 Healthy"


def get_recycle_bin_details(user_folder):
    trash_path = os.path.join(user_folder, ".trash")
    if not os.path.isdir(trash_path):
        return 0, 0
    return get_folder_size(trash_path), count_files(trash_path)


def get_activity_insights(user_folder):
    now = time.time()
    cutoffs = {
        "today": now - 86400,
        "w7":    now - 7   * 86400,
        "w14":   now - 14  * 86400,
        "year":  now - 365 * 86400,
    }
    today_files = w7_added = w14_added = yr_added = 0
    w14_removed = yr_removed = 0

    trash_path = os.path.join(user_folder, ".trash")

    # Uploads — use btime (birth time) where available, else min(mtime, ctime)
    for dirpath, dirnames, filenames in os.walk(user_folder):
        dirnames[:] = [d for d in dirnames if d != ".trash"]
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                st   = os.stat(fp)
                bt   = getattr(st, "st_birthtime", None) or min(st.st_mtime, st.st_ctime)
                size = st.st_size
                if bt >= cutoffs["today"]: today_files += 1
                if bt >= cutoffs["w7"]:   w7_added    += size
                if bt >= cutoffs["w14"]:  w14_added   += size
                if bt >= cutoffs["year"]: yr_added    += size
            except OSError:
                pass

    # Deletions — ctime of files now sitting in .trash
    if os.path.isdir(trash_path):
        for dirpath, _, filenames in os.walk(trash_path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    st   = os.stat(fp)
                    ct   = st.st_ctime
                    size = st.st_size
                    if ct >= cutoffs["w14"]:  w14_removed += size
                    if ct >= cutoffs["year"]: yr_removed  += size
                except OSError:
                    pass

    return {
        "today_files": today_files,
        "w7_added":    w7_added,
        "w14_added":   w14_added,
        "w14_removed": w14_removed,
        "yr_added":    yr_added,
        "yr_removed":  yr_removed,
    }


################################
# Dashboard Builder
################################

def build_storage_dashboard(vareon_id):
    user_folder = f"{USERS_PATH}/{vareon_id:08d}"
    # ── Usage ──────────────────────────────────────────────────────────
    used_bytes = get_folder_size(user_folder)
    free_bytes = max(0, STORAGE_QUOTA_BYTES - used_bytes)
    pct        = min(100.0, (used_bytes / STORAGE_QUOTA_BYTES) * 100)
    file_count = count_files(user_folder)

    bar    = build_bar(pct)
    status = get_status(pct)

    # ── Recycle bin ────────────────────────────────────────────────────
    trash_bytes, trash_files = get_recycle_bin_details(user_folder)

    # ── Activity ───────────────────────────────────────────────────────
    act = get_activity_insights(user_folder)

    # ── Assemble (HTML) ────────────────────────────────────────────────
    msg = (
        "<blockquote>"
        "╔════════════════╗\n"
        f"    💽 YOUR STORAGE\n"
        "╚════════════════╝\n"
        "</blockquote>\n"
        f"  {bar}  <b>{pct:.2f}%</b>\n"
        f"  ├ Used  →  <b>{smart_format(used_bytes)}</b> of {STORAGE_QUOTA_LABEL}\n"
        f"  ├ Free  →  <b>{smart_format(free_bytes)}</b>\n"
        f"  ├ Files →  <b>{file_count}</b>\n"
        f"  └ State →  {status}\n\n"
        "╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌\n"
        f"  🗑 Bin  →  <b>{smart_format(trash_bytes)}</b>  ·  {trash_files} files\n"
        "╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌\n\n"
        "  <b>📊 ACTIVITY LOG</b>\n"
        f"  ┌ Today:      +{act['today_files']} files\n"
        f"  ├ Week:       +{smart_format(act['w7_added'])}\n"
        f"  ├ 2 Weeks:  +{smart_format(act['w14_added'])} ▲  −{smart_format(act['w14_removed'])} ▼\n"
        f"  └ Year:         +{smart_format(act['yr_added'])} ▲  −{smart_format(act['yr_removed'])} ▼\n"
        "╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌\n\n"
        "  💡 <i>Use /trash to empty the bin</i>"
    )
    return msg


def build_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh",   callback_data="refresh_storage")],
        [InlineKeyboardButton("🗑 Empty Bin", callback_data="trash_action:empty_bin")],
        [InlineKeyboardButton("❌ Close",     callback_data="_common_menu:close:storage", style="danger")],
    ])


################################
# Handlers
################################

async def storage(update: Update, context: CallbackContext):
    user_id      = update.message.from_user.id
    session_data = sessions.get(user_id)

    if not session_data:
        await update.message.reply_text("❌ Please login first using /login.")
        return
    if report_mode.get(user_id, False):
        return

    vareon_id = session_data.get("vareon_id")
    if not vareon_id:
        await update.message.reply_text("❌ Session corrupted. Please login again.")
        return

    try:
        dashboard = build_storage_dashboard(vareon_id)
    except Exception as e:
        logger.error(f"Storage fetch failed for {session_data}: {e}")
        await update.message.reply_text("❌ Failed to fetch storage details.")
        return

    keyboard = build_keyboard()
    sent = await update.message.reply_text(
        dashboard,
        parse_mode="HTML",
        reply_markup=keyboard,
    )

    context.user_data.setdefault("stored_messages", {})[
        f"{update.message.chat_id}_{sent.message_id}"
    ] = {"text": dashboard, "markup": keyboard.to_dict()}

    return ConversationHandler.END


async def refresh_storage(update: Update, context: CallbackContext):
    query      = update.callback_query
    await query.answer()

    user_id    = query.from_user.id
    chat_id    = query.message.chat_id
    message_id = query.message.message_id

    if user_id not in sessions:
        await edit_message_if_changed(
            context, chat_id, message_id,
            "❌ Please login again to view storage.",
            parse_mode="HTML",
        )
        return

    session_data = sessions.get(user_id)
    vareon_id    = session_data.get("vareon_id")

    try:
        new_text = build_storage_dashboard(vareon_id)
        keyboard = build_keyboard()
        await edit_message_if_changed(
            context, chat_id, message_id,
            new_text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        logger.debug(f"Refreshed storage dashboard for user {user_id}")
    except Exception as e:
        logger.error(f"Failed to refresh storage for user {user_id}: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Failed to refresh storage details. Please try again.",
            parse_mode="HTML",
        )


async def empty_bin(update: Update, context: CallbackContext):
    """Placeholder — full implementation coming later."""
    query = update.callback_query
    await query.answer("🚧 This feature is coming soon!", show_alert=True)