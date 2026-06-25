from collections import defaultdict
import os
import shutil
import asyncio
from datetime import timedelta
user_locks = defaultdict(asyncio.Lock)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ConversationHandler, CallbackContext, ContextTypes)
from main.utils import register_path
from main.state import sessions
from main.config import MOVE_FOLDER, logger, USERS_PATH
from main.state import sessions
from features.myfiles.browse import refresh_folder_menu

MOVE_ITEMS_PER_PAGE = 30

async def start_move_folder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    try:
        _, uid = query.data.split("|", 1)
    except ValueError:
        await query.edit_message_text("⚠️ Invalid move request.")
        return ConversationHandler.END

    path = context.user_data.get("path_map", {}).get(uid)
    if not path or not os.path.exists(path) or not os.path.isdir(path):
        await query.edit_message_text("❌ Folder not found.")
        return ConversationHandler.END

    context.user_data["move_folder_uid"] = uid
    context.user_data["move_folder_path"] = path
    context.user_data["current_mode"] = "move_folder"
    vareon_id = sessions[(update.effective_user.id if update.effective_user else "unknown")].get("vareon_id")
    base_path = f"{USERS_PATH}/{vareon_id}"
    context.user_data["move_path_stack"] = [base_path]

    await query.edit_message_text(
        f"📂 Moving folder: {os.path.basename(path)}\n\nSelect a target folder to move to:"
    )
    await show_move_folder_menu(update, context)
    return MOVE_FOLDER

def get_move_paginated_buttons(buttons, page=1):
    """Pagination for move folder menu"""
    if not buttons:
        return [[InlineKeyboardButton("📂 No subfolders here", callback_data="noop")]], 1, 1, []

    total_items = len(buttons)
    total_pages = max(1, (total_items + MOVE_ITEMS_PER_PAGE - 1) // MOVE_ITEMS_PER_PAGE)
    page = max(1, min(page, total_pages))

    start = (page - 1) * MOVE_ITEMS_PER_PAGE
    end = start + MOVE_ITEMS_PER_PAGE
    page_buttons = buttons[start:end]

    nav_buttons = []
    if total_pages > 1:
        prev_text = "◀️ Prev" if page > 1 else " "
        next_text = "Next ▶️" if page < total_pages else " "
        nav_buttons = [
            InlineKeyboardButton(prev_text, callback_data=f"move_page_prev|{page}"),
            InlineKeyboardButton(f"[{page}/{total_pages}]", callback_data="noop"),
            InlineKeyboardButton(next_text, callback_data=f"move_page_next|{page}"),
        ]

    return page_buttons, page, total_pages, nav_buttons


async def move_page_prev(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("|")[1])
    if page > 1:
        context.user_data["move_current_page"] = page - 1
    await show_move_folder_menu(update, context)


async def move_page_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("|")[1])
    context.user_data["move_current_page"] = page + 1
    await show_move_folder_menu(update, context)


async def show_move_folder_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query if update.callback_query else None
    if query:
        await query.answer()
        chat_id = query.message.chat_id
    else:
        chat_id = update.message.chat_id

    user_id = (update.effective_user.id if update.effective_user else "unknown")
    if user_id not in sessions:
        await context.bot.send_message(chat_id, "❌ Please login first.")
        return

    path_stack = context.user_data.get("move_path_stack", [])
    if not path_stack:
        await context.bot.send_message(chat_id, "❌ Cannot navigate.")
        return

    current_path = path_stack[-1]
    if not os.path.exists(current_path):
        await context.bot.send_message(chat_id, f"❌ Folder not found: {current_path}")
        return

    items = [e for e in os.scandir(current_path) if e.is_dir() and not e.name.startswith('.')]
    items.sort(key=lambda e: e.name.lower())

    # Build all folder buttons first
    buttons = []
    for entry in items:
        uid = register_path(context, entry.path)
        buttons.append([
            InlineKeyboardButton(f"📁 {entry.name}", callback_data=f"navigate_move|{uid}")
        ])

    # Apply pagination
    current_page = context.user_data.get("move_current_page", 1)
    page_buttons, current_page, total_pages, nav_buttons = get_move_paginated_buttons(buttons, current_page)
    context.user_data["move_current_page"] = current_page

    # Assemble final keyboard
    keyboard = page_buttons

    if len(path_stack) > 1:
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="navigate_move_back")])

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("🚚 Move Here", callback_data="move_here")])

    mode = context.user_data.get("current_mode")
    if mode == "multi_move":
        count = context.user_data.get("multi_move_count", 0)
        text = f"📂 Select target folder to move **{count}** item{'s' if count != 1 else ''}."
    else:
        item_name = os.path.basename(
            context.user_data.get("move_file_path", "") or
            context.user_data.get("move_folder_path", "")
        )
        text = f"📂 Select target folder for: `{item_name}`"

    reply_markup = InlineKeyboardMarkup(keyboard)

    if query:
        await query.edit_message_text(text=text, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=reply_markup)
        
async def navigate_move_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        logger.error("❌ Callback query missing in navigate_move_folder.")
        return

    await query.answer()
    user_id = query.from_user.id
    action, uid = query.data.split("|", 1)

    if action != "navigate_move":
        await query.edit_message_text("❌ Invalid action.")
        return

    new_path = context.user_data.get("path_map", {}).get(uid)
    if not new_path or not os.path.exists(new_path):
        await query.edit_message_text("❌ Folder not found.")
        return

    vareon_id = sessions[user_id].get("vareon_id")
    user_base_dir = os.path.abspath(f"{USERS_PATH}/{vareon_id}")
    if not os.path.abspath(new_path).startswith(user_base_dir):
        await query.edit_message_text("❌ Access denied.")
        return

    context.user_data["move_path_stack"].append(new_path)
    await show_move_folder_menu(update, context)

async def navigate_move_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    path_stack = context.user_data.get("move_path_stack", [])
    if len(path_stack) > 1:
        path_stack.pop()
    current_path = path_stack[-1] if path_stack else None

    if not current_path or not os.path.exists(current_path):
        await query.edit_message_text("❌ Path not found.")
        return

    await show_move_folder_menu(update, context)

async def move_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = (update.effective_user.id if update.effective_user else "unknown")

    mode = context.user_data.get("current_mode")
    vareon_id = sessions[user_id].get("vareon_id")

    target_path = context.user_data.get("move_path_stack", [])[-1]
    if not target_path or not os.path.exists(target_path):
        await query.edit_message_text("❌ Target folder not found.")
        return ConversationHandler.END

    moved_count = 0
    errors = []

    if mode == "multi_move":
        # ──────────────────────────────
        #   MULTI-MOVE LOGIC
        # ──────────────────────────────
        selected_uids = context.user_data.get("multi_move_uids", [])
        if not selected_uids:
            await query.edit_message_text("❌ No items to move (session expired).")
            return ConversationHandler.END

        for uid in selected_uids:
            source_path = context.user_data.get("path_map", {}).get(uid)
            if not source_path or not os.path.exists(source_path):
                errors.append(f"Item {uid}: source not found")
                continue

            if os.path.isdir(source_path):
                if os.path.abspath(target_path).startswith(os.path.abspath(source_path)):
                    errors.append(f"Cannot move folder '{os.path.basename(source_path)}' into itself or subfolder")
                    continue

            new_path = os.path.join(target_path, os.path.basename(source_path))

            if os.path.exists(new_path):
                errors.append(f"'{os.path.basename(source_path)}' already exists in target")
                continue

            try:
                shutil.move(source_path, new_path)
                context.user_data["path_map"][uid] = new_path
                moved_count += 1
                logger.info("Moved (multi): %s → %s", source_path, new_path)
            except Exception as e:
                logger.error("Move failed for %s: %s", source_path, str(e))
                errors.append(f"Failed to move '{os.path.basename(source_path)}': {str(e)}")

        context.user_data.pop("multi_move_uids", None)
        context.user_data.pop("multi_move_count", None)
        context.user_data.pop("current_mode", None)
        context.user_data.pop('multi_select_mode', None)
        context.user_data.pop('selected_uids', None)

        if moved_count == len(selected_uids):
            msg = f"✅ Moved **{moved_count}** item{'s' if moved_count != 1 else ''} successfully."
        else:
            msg = f"⚠️ Moved **{moved_count}** of **{len(selected_uids)}** items.\n"
            if errors:
                msg += "\nErrors:\n• " + "\n• ".join(errors)

        await query.edit_message_text(msg, parse_mode="Markdown")

        target_path = context.user_data.get("move_path_stack", [])[-1]

        if target_path and os.path.exists(target_path):
            root = f"{USERS_PATH}/{vareon_id}"
            new_stack = [root]
            if target_path != root:
                rel = os.path.relpath(target_path, root)
                parts = rel.split(os.sep)
                current = root
                for part in parts:
                    if part:
                        current = os.path.join(current, part)
                        new_stack.append(current)

            context.user_data["path_stack"] = new_stack
            context.user_data["current_page"] = 1
            context.user_data["last_action"] = "refresh"

            await refresh_folder_menu(update, context, edit_text=False)
        else:
            # fallback - stay where we were (rare)
            context.user_data["last_action"] = "refresh"
            await refresh_folder_menu(update, context, edit_text=False)

        return ConversationHandler.END

    elif mode in ("move_file", "move_folder"):
        # ──────────────────────────────
        #   EXISTING SINGLE MOVE LOGIC
        # ──────────────────────────────
        if mode == "move_folder":
            source_path = context.user_data.get("move_folder_path")
            uid = context.user_data.get("move_folder_uid")
            item_type = "folder"
        else:
            source_path = context.user_data.get("move_file_path")
            uid = context.user_data.get("move_file_uid")
            item_type = "file"

        if not source_path or not uid or not os.path.exists(source_path):
            await query.edit_message_text("❌ Source not found.")
            return ConversationHandler.END

        if os.path.isdir(source_path) and os.path.abspath(target_path).startswith(os.path.abspath(source_path)):
            await query.edit_message_text("❌ Cannot move a folder into itself or its subdirectories.")
            return MOVE_FOLDER

        new_path = os.path.join(target_path, os.path.basename(source_path))
        if os.path.exists(new_path):
            await query.edit_message_text("⚠️ A file or folder with that name already exists in the target location.")
            return MOVE_FOLDER

        try:
            shutil.move(source_path, new_path)
            context.user_data["path_map"][uid] = new_path

            # ─── Navigate to destination folder ─────────────────────────────────
            root = f"{USERS_PATH}/{vareon_id}"
            new_stack = [root]
            if target_path != root:
                rel = os.path.relpath(target_path, root)
                parts = rel.split(os.sep)
                current = root
                for part in parts:
                    if part:
                        current = os.path.join(current, part)
                        new_stack.append(current)

            context.user_data["path_stack"] = new_stack
            context.user_data["current_page"] = 1
            context.user_data["last_action"] = "refresh"

            await query.edit_message_text(
                f"✅ {item_type.capitalize()} moved successfully to `{os.path.basename(target_path)}`",
                parse_mode="Markdown"
            )

            await refresh_folder_menu(update, context, edit_text=False)
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to move {item_type}: {str(e)}")

    else:
        await query.edit_message_text("❌ Invalid move mode.")
        return ConversationHandler.END

    return ConversationHandler.END

async def multi_move(update, context, vareon_id):
    query = update.callback_query
    selected_uids = context.user_data.get('selected_uids', set())
    if not selected_uids:
        await query.edit_message_text("❌ No items selected.")
        return

    context.user_data["multi_move_uids"] = list(selected_uids)
    context.user_data["multi_move_count"] = len(selected_uids)
    context.user_data["move_path_stack"] = [f"{USERS_PATH}/{vareon_id}"]
    context.user_data["current_mode"] = "multi_move"

    count = len(selected_uids)
    await query.edit_message_text(
        f"🚚 Moving **{count}** item{'s' if count > 1 else ''}\n\nSelect target folder:",
        parse_mode="Markdown"
    )
    await show_move_folder_menu(update, context)