from collections import defaultdict
import os
import asyncio
from datetime import datetime, timedelta
user_locks = defaultdict(asyncio.Lock)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ConversationHandler, CallbackContext, ContextTypes)
from main.utils import register_path, format_size
from main.config import logger, USERS_PATH
from main.state import sessions
ITEMS_PER_PAGE = 30

def get_path_from_uid(context, uid):
    return context.user_data.get("path_map", {}).get(uid)

def get_paginated_buttons(buttons, page=1):
    """Split buttons into pages and return current page + nav buttons"""
    if not buttons:
        return [[InlineKeyboardButton("📂 Folder is empty", callback_data="noop")]], 1, 1

    total_items = len(buttons)
    total_pages = max(1, (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(1, min(page, total_pages))

    start = (page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_buttons = buttons[start:end]

    # Navigation row
    nav_buttons = []
    if total_pages > 1:
        prev_text = "◀️ Prev" if page > 1 else " "
        next_text = "Next ▶️" if page < total_pages else " "
        nav_buttons = [InlineKeyboardButton(prev_text, callback_data=f"page_prev|{page}"),
                       InlineKeyboardButton(f"[{page}/{total_pages}]", callback_data="noop"),
                       InlineKeyboardButton(next_text, callback_data=f"page_next|{page}")]

    return page_buttons, page, total_pages, nav_buttons

async def page_prev(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("|")[1])
    if page > 1:
        context.user_data['current_page'] = page - 1
        await refresh_folder_menu(update, context, edit_text=True)


async def page_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("|")[1])
    # total_pages is not stored, but we can refresh and let get_paginated_buttons handle bounds
    context.user_data['current_page'] = page + 1
    await refresh_folder_menu(update, context, edit_text=True)
    
async def refresh_folder_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_text: bool = True):

    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat

    logger.info(
        "refresh_folder_menu START | edit_text=%s | mode=%s | user=%s",
        edit_text,
        "select" if context.user_data.get("multi_select_mode") else "normal",
        user.id if user else "unknown"
    )

    path_stack = context.user_data.get("path_stack", [])

    if not path_stack:
        logger.warning("No path_stack in refresh_folder_menu")

        if query:
            await query.answer()
            await query.edit_message_text("❌ No current folder.")
        else:
            await context.bot.send_message(chat.id, "❌ No current folder.")
        return

    current_path = path_stack[-1]
    context.user_data.setdefault("path_map", {})

    # ── READ DIRECTORY ─────────────────────────────

    try:
        entries = list(os.scandir(current_path))
        entries.sort(key=lambda e: (not e.is_dir(), e.name.lower()))
    except Exception as e:
        logger.error("Directory read failed: %s", str(e))

        if query:
            await query.edit_message_text("❌ Cannot read folder.")
        else:
            await context.bot.send_message(chat.id, "❌ Cannot read folder.")
        return

    raw_items = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        uid = register_path(context, entry.path)
        if not uid:
            continue

        context.user_data["path_map"][uid] = entry.path
        raw_items.append((uid, entry.name, entry.is_dir()))

    total_items = len(raw_items)

    logger.info("Raw items in folder: %d", total_items)

    # ── PAGINATION ─────────────────────────────

    current_page = context.user_data.get("current_page", 1)

    if context.user_data.get("current_path") != current_path:
        current_page = 1

    total_pages = max(1, (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)

    current_page = max(1, min(current_page, total_pages))

    start = (current_page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE

    page_items = raw_items[start:end]

    # ── BUILD KEYBOARD ─────────────────────────
    keyboard = []

    is_select_mode = context.user_data.get("multi_select_mode", False)
    selected_uids = context.user_data.get("selected_uids", set())
    COMPRESSED_EXTS = ('.zip', '.rar', '.tar', '.7z', '.gz', '.bz2', '.xz')

    for uid, name, is_folder in page_items:

        if is_folder:
            text = f"📁 {name}"
            callback = f"open|{uid}"
        else:
            # Check if file is a compressed archive
            if name.lower().endswith(COMPRESSED_EXTS):
                icon = "🗜️"
            else:
                icon = "💾"
            
            text = f"{icon} {name}"
            callback = f"file|{uid}"

        if is_select_mode:
            prefix = "☑️ " if uid in selected_uids else ""
            text = prefix + text
            callback = f"select|{uid}"

        keyboard.append([InlineKeyboardButton(text, callback_data=callback)])
    # ── PAGE NAVIGATION ────────────────────────

    if total_pages > 1:

        prev_btn = InlineKeyboardButton(
            "◀️ Prev" if current_page > 1 else " ",
            callback_data=f"page_prev|{current_page}"
        )

        next_btn = InlineKeyboardButton(
            "Next ▶️" if current_page < total_pages else " ",
            callback_data=f"page_next|{current_page}"
        )

        keyboard.append([
            prev_btn,
            InlineKeyboardButton(f"[{current_page}/{total_pages}]", callback_data="noop"),
            next_btn
        ])

    # ── BACK BUTTON ────────────────────────────

    if len(path_stack) > 1:
        keyboard.insert(0, [InlineKeyboardButton("⬅️ Back", callback_data="back", style="primary")])

    # ── EXTRA CONTROLS ─────────────────────────
    if is_select_mode:
        num_selected = len(selected_uids)
        page_uids = {uid for uid, _, _ in page_items}

        # 1. Analyze selected files
        has_compressed = False
        has_regular = False

        for uid in selected_uids:
            file_path = get_path_from_uid(context, uid) 
            
            if file_path:
                if os.path.isfile(file_path) and file_path.lower().endswith(COMPRESSED_EXTS):
                    has_compressed = True
                else:
                    has_regular = True

        if page_uids and page_uids.issubset(selected_uids):
            select_text = "🗂️ Deselect All"
        else:
            select_text = "🗂️ Select All"
        keyboard.append([InlineKeyboardButton(select_text, callback_data="select_all", style="success")])
        keyboard.append([
            InlineKeyboardButton(f"🗑️ Delete ({num_selected})", callback_data="multi_delete", style="primary"),
            InlineKeyboardButton(f"🚚 Move ({num_selected})", callback_data="multi_move", style="primary")
        ])

        if has_compressed and not has_regular:
            keyboard.append([InlineKeyboardButton(f"🔓 Extract Selected ({num_selected})", callback_data="multi_extract", style="primary")])
        
        if num_selected > 0:
            keyboard.append([InlineKeyboardButton(f"🔒 Compress Selected ({num_selected})", callback_data="multi_compress", style="primary")])

        keyboard.append([InlineKeyboardButton("❌ Exit Selection", callback_data="multi_cancel", style="danger")])
    else:
        vareon_id = sessions[user.id]["vareon_id"]
        main_directory = f"{USERS_PATH}/{vareon_id}"

        if current_path == main_directory:
            keyboard.insert(0, [InlineKeyboardButton("🆕 New Folder", callback_data="new_folder", style="primary")])

        else:
            current_uid = register_path(context, current_path)
            if current_uid:
                context.user_data["path_map"][current_uid] = current_path

                keyboard.append([
                    InlineKeyboardButton("✏️ Rename Folder", callback_data=f"rename_folder|{current_uid}"),
                    InlineKeyboardButton("🚚 Move Folder", callback_data=f"move_folder|{current_uid}")
                ])
                keyboard.append([
                    InlineKeyboardButton("🚮 Delete This Folder", callback_data=f"delete_folder|{current_uid}"),
                    InlineKeyboardButton("🆕 New Folder", callback_data="new_folder")
                ])

        # Common buttons for both root and subfolders
        keyboard.append([InlineKeyboardButton("📌 Multiple Selection", callback_data="multi_select", style="success")])
        keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh", style="primary")])
        keyboard.append([InlineKeyboardButton("❌ Close", callback_data="_common_menu:close:myfiles", style="danger")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    # ── SAVE STATE ─────────────────────────────
    context.user_data["current_page"] = current_page
    context.user_data["current_path"] = current_path

    folder_name = os.path.basename(current_path)

    # ── FOLDER METADATA ─────────────────────────────
    def get_folder_size(path):
        total = 0
        try:
            for dirpath, dirnames, filenames in os.walk(path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    if os.path.exists(fp):
                        total += os.path.getsize(fp)
        except Exception:
            pass
        return total

    # Get size
    try:
        raw_size = get_folder_size(current_path)
        folder_size = format_size(raw_size) if raw_size > 0 else "0B"
    except Exception:
        folder_size = "Unknown"

    # Get created at
    try:
        created_ts = os.path.getctime(current_path)
        created_at = datetime.fromtimestamp(created_ts).strftime("%d %B %Y, %I:%M:%S %p")
    except Exception:
        created_at = "Unknown"

    # Get relative location (e.g. 24643159/home)
    try:
        base_dir = context.user_data.get("path_stack", [current_path])[0]
        relative_location = os.path.relpath(current_path, os.path.dirname(base_dir))
    except Exception:
        relative_location = folder_name

    # ── BUILD TEXT ─────────────────────────────
    if context.user_data.get("last_action") == "refresh":
        text = (
            f"📂 Your Folder: {folder_name}\n"
            f"📍 Location: {relative_location}\n"
            f"📦 Size: {folder_size}\n"
            f"📅 Created at: {created_at}\n\n"
            f"✅ Folder refreshed at {datetime.now().strftime('%H:%M:%S')}"
        )
    else:
        text = (
            f"📂 Your Folder: {folder_name}\n"
            f"📍 Location: {relative_location}\n"
            f"📦 Size: {folder_size}\n"
            f"📅 Created at: {created_at}"
        )

    logger.info("Preparing menu | rows=%d | page=%d/%d", len(keyboard), current_page, total_pages)
    # ── SEND OR EDIT MESSAGE ───────────────────

    try:

        if query and query.message:

            await query.answer()

            if edit_text:
                await query.edit_message_text(
                    text=text,
                    reply_markup=reply_markup
                )
            else:
                await query.edit_message_reply_markup(
                    reply_markup=reply_markup
                )

        else:

            await context.bot.send_message(
                chat_id=chat.id,
                text=text,
                reply_markup=reply_markup
            )

    except Exception as e:

        logger.error("Failed to update folder menu: %s", str(e))

    logger.info("refresh_folder_menu END")
    