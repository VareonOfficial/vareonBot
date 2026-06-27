from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from datetime import datetime
from main.state import sessions
from features.trash.helper import (_get_db ,_now_time, _get_page, _set_page, _get_select_state, _format_size, 
                                   _auto_delete_days, _clear_select_state, _file_icon)
from features.shared.storage import smart_format, get_recycle_bin_details
from main.config import USERS_PATH
PAGINATION_SIZE = 30

# ─── Core render function ─────────────────────────────────────────────────────
async def _render_trash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Single function that builds and sends/edits the trash menu.
    Handles both normal mode and multi-select mode.
    Always reads current page and select state from context.user_data.
    """
    telegram_user_id = update.effective_user.id
    vareon_id = sessions.get(telegram_user_id, {}).get("vareon_id")

    conn, cursor = _get_db()
    try:
        cursor.execute(
            "SELECT id, filename, size FROM trash_files WHERE vareon_id = ? ORDER BY deleted_at DESC",
            (vareon_id,)
        )
        all_rows = cursor.fetchall()
    finally:
        conn.close()

    # ── Pagination math ──
    total_files  = len(all_rows)
    total_pages  = max((total_files + PAGINATION_SIZE - 1) // PAGINATION_SIZE, 1)
    page         = _get_page(context)

    # Clamp page in case files were deleted and page is now out of range
    page = max(0, min(page, total_pages - 1))
    _set_page(context, page)

    page_rows    = all_rows[page * PAGINATION_SIZE : (page + 1) * PAGINATION_SIZE]

    # ── State ──
    select_mode, selected_ids = _get_select_state(context)
    selected_count = len(selected_ids)

    # ── Total storage size (all files, not just this page) ──
    user_folder = f"{USERS_PATH}/{vareon_id}"
    trash_bytes, trash_files = get_recycle_bin_details(user_folder)

    # ── Message text ──
    text = (
        f"🗑️ <b>Your Trash</b>\n"
        f"Storage used: <b>{smart_format(trash_bytes)}</b>  ·  {trash_files} files\n\n"
        f"<i>✅ Menu refreshed at {_now_time()}</i>"
    )

    if total_pages > 1:
        text += f"\n<i>Page {page + 1} of {total_pages}</i>"

    if not all_rows:
        text += "\n\n<i>Trash is empty.</i>"

    # ── Build keyboard ──
    keyboard = []

    # File buttons — behaviour changes based on mode
    for row in page_rows:
        file_id  = str(row["id"])
        icon     = _file_icon(row["filename"])
        name     = row["filename"]

        if select_mode:
            # Show checkbox + toggle callback
            check = "✅" if file_id in selected_ids else ""
            label = f"{check} {icon} {name}"
            cb    = f"trash_toggle:{file_id}"
        else:
            # Normal mode — opens detail view
            label = f"{icon} {name}"
            cb    = f"trash_file:{file_id}"

        keyboard.append([InlineKeyboardButton(label, callback_data=cb)])

    # ── Pagination row ──
    # Only shown when there is more than one page
    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"trash_page:{page - 1}"))
        # Page indicator (non-clickable)
        nav_row.append(InlineKeyboardButton(f"· {page + 1}/{total_pages} ·", callback_data="trash_noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"trash_page:{page + 1}"))
        keyboard.append(nav_row)

    # ── Bottom action rows ──
    if select_mode:
        # Multi-select mode actions
        keyboard.append([
            InlineKeyboardButton(
                f"♻️ Restore Selected ({selected_count})",
                callback_data="trash_select_action:restore_selected",
                style="success"
            ),
        ])
        keyboard.append([
            InlineKeyboardButton(
                f"🗑️ Delete Selected ({selected_count})",
                callback_data="trash_select_action:delete_selected",
                style="primary"
            ),
        ])
        keyboard.append([
            InlineKeyboardButton("❌ Cancel Selection", callback_data="trash_action:cancel_select", style="danger"),
        ])
    else:
        # Normal mode actions
        keyboard.append([
            InlineKeyboardButton("🗑️ Empty Bin",    callback_data="trash_action:empty_bin", style="success"),
            InlineKeyboardButton("📌 Multi Select", callback_data="trash_action:multi_select", style="primary"),
        ])
        keyboard.append([
            InlineKeyboardButton("♻️ Restore All",  callback_data="trash_action:restore_all", style="success"),
            InlineKeyboardButton("🔄 Refresh",      callback_data="trash_action:refresh", style="primary"),
            InlineKeyboardButton("❌ Close",        callback_data="trash_action:close", style="danger"),
        ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    # ── Send or edit ──
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")


# ─── /trash entry point ───────────────────────────────────────────────────────

async def trash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /trash command — resets page to 0 and clears select state, then renders.
    """
    _set_page(context, 0)
    _clear_select_state(context)
    await _render_trash(update, context)

async def trash_file_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Shows detail card for a single trash file.
    Triggered by callback_data = "trash_file:<id>"
    Only reachable in normal (non-select) mode.
    """
    query = update.callback_query
    await query.answer()

    file_id = query.data.split(":", 1)[1]

    conn, cursor = _get_db()
    try:
        cursor.execute(
            "SELECT * FROM trash_files WHERE id = ?",
            (file_id,)
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    if not row:
        await query.edit_message_text("⚠️ File not found. It may have already been deleted.")
        return

    size_str     = _format_size(row["size"])
    deleted_dt   = datetime.fromisoformat(row["deleted_at"])
    deleted_fmt  = deleted_dt.strftime("%d %B %Y, %I:%M %p")
    days_left    = _auto_delete_days(row["deleted_at"])
    path_parts   = row["original_path"].split("/")
    display_path = "/" + "/".join(path_parts[path_parts.index("users") + 2:])

    # Current page so Back button returns to correct page
    page = _get_page(context)

    text = (
        f"📄 <b>Name</b> : {row['filename']}\n\n"
        f"<b>Size:</b> {size_str}\n"
        f"<b>Original location:</b> <code>{display_path}</code>\n"
        f"<b>👤 User ID:</b> {row['telegram_id']}\n"
        f"<b>📅 Deleted:</b> {deleted_fmt}\n"
        f"<b>⏳ Auto-delete in:</b> {days_left} days\n\n"
        f"<i>✅ Menu refreshed at {_now_time()}</i>"
    )

    keyboard = [
        [InlineKeyboardButton("♻️ Restore",       callback_data=f"trash_file_action:restore:{file_id}", style="success")],
        [InlineKeyboardButton("🗑️ Delete Forever", callback_data=f"trash_file_action:delete_forever:{file_id}", style="danger")],
        [InlineKeyboardButton("◀️ Back",           callback_data=f"trash_page:{page}", style="primary")],
    ]

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )

async def _show_confirm(query, action_key: str, message: str):
    """Replaces current message with a Yes/No confirmation card."""
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, Delete", callback_data=f"trash_confirm:{action_key}"),
            InlineKeyboardButton("❌ Cancel",      callback_data="trash_action:refresh"),
        ]
    ]
    await query.edit_message_text(
        f"{message}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )