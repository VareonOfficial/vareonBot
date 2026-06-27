from telegram import Update
from telegram.ext import ContextTypes
from main.state import sessions
from main.config import USERS_PATH
from features.trash.helper import _set_page,_clear_select_state
from features.trash.restore_trash import _do_restore_single, _do_restore_all, _do_restore_selected
from features.trash.delete_trash import _do_delete_single, _do_empty_bin, _do_delete_selected
from features.trash.trash import _render_trash, _show_confirm
from features.shared.storage import get_recycle_bin_details

async def trash_page_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles Prev / Next page buttons.
    callback_data = "trash_page:<page_number>"
    """
    query = update.callback_query
    await query.answer()

    page = int(query.data.split(":", 1)[1])
    _set_page(context, page)
    await _render_trash(update, context)


# ─── Toggle handler (multi-select) ───────────────────────────────────────────

async def trash_toggle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Toggles a file's selected state in multi-select mode.
    callback_data = "trash_toggle:<file_id>"
    """
    query = update.callback_query

    file_id = query.data.split(":", 1)[1]

    selected_ids = context.user_data.setdefault("trash_selected", set())

    if file_id in selected_ids:
        selected_ids.discard(file_id)
    else:
        selected_ids.add(file_id)
        
    await _render_trash(update, context)

# ─── Action Router ────────────────────────────────────────────────────────────

async def trash_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Routes top-level trash action buttons.
    callback_data = "trash_action:<action>"
    """
    query = update.callback_query
    await query.answer()
    telegram_user_id = update.effective_user.id
    vareon_id = sessions.get(telegram_user_id, {}).get("vareon_id")


    action = query.data.split(":", 1)[1]

    if action == "refresh":
        await _render_trash(update, context)

    elif action == "close":
        await query.delete_message()

    elif action == "multi_select":
        # Enter multi-select mode, keep current page
        context.user_data["trash_select_mode"] = True
        context.user_data["trash_selected"]    = set()
        await _render_trash(update, context)

    elif action == "cancel_select":
        # Exit multi-select mode, keep current page
        _clear_select_state(context)
        await _render_trash(update, context)

    elif action == "empty_bin":
        user_folder = f"{USERS_PATH}/{vareon_id}"
        _, trash_files = get_recycle_bin_details(user_folder)  
        if trash_files > 0:
            await _show_confirm(query, "empty_bin", f"⚠️Are you sure you want to permanently delete these {trash_files} items? This cannot be undone.")
         
    elif action == "restore_all":
        await _do_restore_all(update, query, context, vareon_id)
        await _render_trash(update, context)

    elif action == "noop":
        # The page indicator button "· 2/5 ·" — do nothing
        pass

async def trash_select_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Routes multi-select bulk action buttons.
    callback_data = "trash_select_action:<action>"
    """
    query = update.callback_query
    await query.answer()

    action       = query.data.split(":", 1)[1]
    selected_ids = context.user_data.get("trash_selected", set())

    if not selected_ids:
        await query.answer("⚠️ No files selected.", show_alert=True)
        return

    if action == "restore_selected":
        await _do_restore_selected(update, query, context, selected_ids)
        await _render_trash(update, context)
    elif action == "delete_selected":
        await _show_confirm(query, "delete_selected", f"⚠️Are you sure you want to permanently delete these {len(selected_ids)} items? This cannot be undone.")


async def trash_file_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Routes per-file action buttons (Restore / Delete Forever).
    callback_data = "trash_file_action:<action>:<file_id>"
    """
    query = update.callback_query
    await query.answer()

    _, action, file_id = query.data.split(":", 2)

    if action == "restore":
        await _do_restore_single(update, query, context, file_id)
        await _render_trash(update, context)

    elif action == "delete_forever":
        await _show_confirm(query, f"delete_single:{file_id}", "🗑️ Are you sure you want to permanently delete this file? This cannot be undone.")


async def trash_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles confirmation Yes button.
    callback_data = "trash_confirm:<action_key>"
    """
    query = update.callback_query
    await query.answer()

    telegram_user_id = update.effective_user.id
    vareon_id = sessions.get(telegram_user_id, {}).get("vareon_id")

    action_key = query.data.split(":", 1)[1]

    if action_key == "empty_bin":
        await _do_empty_bin(update, query, context, vareon_id)

    elif action_key == "delete_selected":
        selected_ids = context.user_data.get("trash_selected", set())
        await _do_delete_selected(update, query, context, selected_ids)

    elif action_key.startswith("delete_single:"):
        file_id = action_key.split(":", 1)[1]
        await _do_delete_single(update, query, context, file_id)

    await _render_trash(update, context)