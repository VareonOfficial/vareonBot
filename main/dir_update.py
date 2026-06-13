import asyncio
import os, sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import logging
from main.state import sessions
from main.config import USERS_PATH, VAREON_DB, logger, ITEMS_PER_PAGE
from main.utils import (
    register_path,
    edit_message_if_changed,
    rate_limit_interaction,
)
from utilities.files.files import run_tdl_download
from utilities.myfiles.myfiles import (myfiles_callback)

async def _show_download_folder_menu_inner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Core logic for the folder menu — call this directly to bypass rate limiting."""
    query = update.callback_query if update.callback_query else None
    if query:
        await query.answer()
        chat_id = query.message.chat_id
        message_id = query.message.message_id
    else:
        chat_id = update.message.chat_id
        message_id = None

    path_stack = context.user_data.get("path_stack")

    if not path_stack:
        await query.edit_message_text("❌ Navigation stack missing.")
        return

    current_path = path_stack[-1]
    if not os.path.exists(current_path):
        await context.bot.send_message(chat_id, f"❌ Error: Folder `{current_path}` does not exist.")
        return

    folder_pages = context.user_data.setdefault("folder_page_map", {})
    current_page = folder_pages.get(current_path, 1)
    
    # Get all folders in current directory
    all_folders = []
    try:
        items = sorted(os.listdir(current_path))
        for item in items:
            full_path = os.path.join(current_path, item)
            if os.path.isdir(full_path) and item.strip() and not item.startswith("."):
                all_folders.append((item, full_path))
    except Exception as e:
        logger.error(f"[FOLDER_MENU] Error listing directory: {e}")
        await context.bot.send_message(chat_id, f"❌ Error reading folder: {e}")
        return

    # Calculate pagination
    total_folders = len(all_folders)
    total_pages = max(1, (total_folders + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    
    # Ensure current page is valid
    if current_page < 1:
        current_page = 1
    if current_page > total_pages:
        current_page = total_pages
    
    # Store pagination info
    folder_pages[current_path] = current_page
    context.user_data["folder_menu_total_pages"] = total_pages
    context.user_data["folder_menu_total_folders"] = total_folders
    
    # Get folders for current page
    start_idx = (current_page - 1) * ITEMS_PER_PAGE
    end_idx = min(start_idx + ITEMS_PER_PAGE, total_folders)
    page_folders = all_folders[start_idx:end_idx]

    # Build keyboard with folders
    keyboard = []
    for item, full_path in page_folders:
        uid = register_path(context, full_path)
        keyboard.append([InlineKeyboardButton(f"📁 {item}", callback_data=f"navigate|{uid}")])

    # Add pagination controls if needed
    if total_folders > ITEMS_PER_PAGE:
        pagination_row = []
        
        # Prev button — only shown when not on first page
        if current_page > 1:
            pagination_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"folder_page|{current_page - 1}"))
        
        # Page indicator always shown
        pagination_row.append(InlineKeyboardButton(f"[{current_page}/{total_pages}]", callback_data="noop"))
        
        # Next button — only shown when not on last page
        if current_page < total_pages:
            pagination_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"folder_page|{current_page + 1}"))
        
        keyboard.append(pagination_row)

    mode = context.user_data.get("current_mode")
    if mode in ["file_download_select", "link_download_select", "set_default", "music_download_select"]:
        keyboard.append([InlineKeyboardButton("📥 Download Here", callback_data="download_here_local", style="primary")])
    if len(path_stack) > 1:
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="navigate_back")])

    # Build text with folder count info
    if total_folders > ITEMS_PER_PAGE:
        text = (f"📂 Path: `{current_path}`\n\n"
                f"📁 Folders: {total_folders} | 📄 Page {current_page}/{total_pages}\n\n"
                f"_Select a folder or choose where to download._")
    else:
        text = f"📂 Path: `{current_path}`\n\n_Select a folder or choose where to download._"

    if query:
        await edit_message_if_changed(context, chat_id, message_id, text, InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return None 
    else:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        context.user_data["last_menu_message_id"] = msg.message_id
        context.user_data.setdefault("stored_messages", {})[f"{chat_id}_{msg.message_id}"] = {
            "text": text,
            "markup": InlineKeyboardMarkup(keyboard).to_dict()
        }
        return msg
    
@rate_limit_interaction
async def show_download_folder_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Public entry point — rate-limited. Delegates to inner logic."""
    msg = await _show_download_folder_menu_inner(update, context)
    return msg

async def navigate_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    logger.info(f"NAV_FOLDER CALLED: {query.data}")
    if not query:
        logger.error("❌ Callback query missing in navigate_folder.")
        return

    await query.answer()
    user_id = query.from_user.id
    action, uid = query.data.split("|", 1)

    if action != "navigate":
        await query.edit_message_text("❌ Invalid action.")
        return

    new_path = context.user_data.get("path_map", {}).get(uid)
    if not new_path or not os.path.exists(new_path):
        return

    user_data = sessions.get(user_id)
    if not user_data:
        await query.edit_message_text("❌ Session expired. Please login again.")
        return

    user_folder = user_data.get('vareon_id')
    user_base_dir = os.path.abspath(f"{USERS_PATH}/{user_folder}")
    if not os.path.abspath(new_path).startswith(user_base_dir):
        await query.edit_message_text("❌ Access denied.")
        return

    context.user_data["path_stack"].append(new_path)
    logger.info(f"SHOW_MENU PATH from the navigate folder: {context.user_data.get('path_stack')[-1]}")
    
    # Reset to page 1 when entering a new folder
    context.user_data["folder_menu_page"] = 1
    
    mode = context.user_data.get("current_mode")
    if mode == "set_default" or context.user_data.get("setting_default_path"):
        context.user_data["current_mode"] = "set_default"
        await _show_download_folder_menu_inner(update, context)
    elif mode == "link_download_select":
        context.user_data["current_mode"] = "link_download_select"
        await _show_download_folder_menu_inner(update, context)
    elif mode == "file_download_select":
        context.user_data["current_mode"] = "file_download_select"
        await _show_download_folder_menu_inner(update, context)
    elif mode == "music_download_select":
        context.user_data["current_mode"] = "music_download_select"
        await _show_download_folder_menu_inner(update, context)
    else:
        context.user_data["current_mode"] = "myfiles"
        await myfiles_callback(update, context)

async def handle_folder_page_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle pagination for folder menu
    """
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if not data.startswith("folder_page|"):
        return
    
    try:
        new_page = int(data.split("|")[1])
        current_path = context.user_data.get("path_stack", [None])[-1]
        context.user_data.setdefault("folder_page_map", {})[current_path] = new_page
        logger.info(f"[FOLDER_PAGE] Navigating to page {new_page}")
        await _show_download_folder_menu_inner(update, context)
    except (ValueError, IndexError) as e:
        logger.error(f"[FOLDER_PAGE] Error parsing page: {e}")
        await query.answer("❌ Invalid page", show_alert=True)

@rate_limit_interaction
async def navigate_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    logger.info(f"NAV_BACK CALLED: {query.data}")
    await query.answer()

    path_stack = context.user_data.get("path_stack")
    if len(path_stack) > 1:
        path_stack.pop()
    current_path = path_stack[-1] if path_stack else None

    if not current_path or not os.path.exists(current_path):
        await query.edit_message_text("❌ Path not found.")
        return

    mode = context.user_data.get("current_mode")
    logger.info(f"SHOW_MENU PATH from navigate back: {context.user_data.get('path_stack')[-1]}")
    if mode in ["file_download_select", "link_download_select", "set_default", "music_download_select"]:
        await _show_download_folder_menu_inner(update, context)
    else:
        await myfiles_callback(update, context)

async def set_download_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    current_path = context.user_data["path_stack"][-1]
    mode = context.user_data.get("current_mode")

    # 🔥 1. DEFAULT PATH SETTING FLOW
    if mode == "set_default":
        try:
            conn = sqlite3.connect(VAREON_DB)
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE user_settings
                SET default_download_enabled = 1,
                    default_download_path = ?
                WHERE telegram_user_id = ?
            """, (current_path, user_id))

            conn.commit()
            conn.close()

            await query.edit_message_text(
                f"✅ Default download location set to:\n`{current_path}`",
                parse_mode="Markdown"
            )
            

        except Exception as e:
            logger.error(f"[SET DEFAULT PATH ERROR] {e}")
            await query.edit_message_text("❌ Failed to set default path.")

        # 🔹 cleanup (VERY IMPORTANT)
        context.user_data.pop("setting_default_path", None)
        context.user_data.pop("current_mode", None)

        return  # 🔥 STOP → prevents download UI from triggering

    # 🔹 2. NORMAL DOWNLOAD FLOW (UNCHANGED)
    file_info = context.user_data.get("file_info", {})
    file_info["download_path"] = current_path
    context.user_data["file_info"] = file_info
    context.user_data["selected_download_path"] = current_path

    await query.edit_message_text(
        f"✅ Download location updated to `{current_path}`",
        parse_mode="Markdown"
    )
        
async def handle_download_here_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    mode = context.user_data.get("current_mode")

    # 🔥 1. DEFAULT PATH FLOW (IMPORTANT ADD THIS)
    if mode == "set_default":
        from utilities.links.links import set_download_location
        await set_download_location(update, context)
        return

    # 🔹 2. LINK DOWNLOAD
    elif mode == "link_download_select":
        from utilities.links.links import download_button_handler        
        await download_button_handler(update, context)
        return

    # 🔹 3. FILE DOWNLOAD
    elif mode == "file_download_select":
        current_path = context.user_data["path_stack"][-1]

        pending = context.user_data.pop("pending_download", None)
        if not pending:
            await query.edit_message_text("❌ No pending file. Please send again.")
            return

        file_name = pending["file_name"]
        link      = pending["forwarded_link"]
        total_size = pending.get("total_size")

        try:
            progress_msg = await query.edit_message_text(
                text=f"📥 Preparing download → `{file_name}`\n"
                    f"📂 Location: `{current_path}`",
                parse_mode="Markdown"
            )

            context.user_data.pop("current_mode", None)
            context.user_data.pop("path_stack", None)
            context.user_data.pop("path_map", None)

            task = asyncio.create_task(
                run_tdl_download(
                    progress_msg=progress_msg,
                    url=link,
                    path=current_path,
                    file_name=file_name,
                    context=context,
                    user_id=query.from_user.id,
                    total_size=total_size
                )
            )
            context.user_data["active_tdl_task"] = task

        except Exception as e:
            logger.error(f"File download failed: {e}", exc_info=True)
            await query.message.reply_text(f"❌ Download setup failed: {str(e)}")
        return

    elif mode == "music_download_select":
        from utilities.music.music import start_music_download
        if query.data == "download_here_tg":
            target = "music_on_telegram"
        else:
            target = None
        path_stack = context.user_data.get("path_stack", [])
        if not path_stack:
            await query.edit_message_text("❌ No folder selected.")
            return

        current_path = path_stack[-1]
        pending = context.user_data.pop("pending_music", None)

        if not pending:
            await query.edit_message_text("❌ No pending music task.")
            return

        # cleanup
        context.user_data.pop("current_mode", None)
        context.user_data.pop("path_stack", None)
        context.user_data.pop("path_map", None)
        
        await query.message.delete()

        task = asyncio.create_task(
            start_music_download(
                link=pending["link"],
                download_path=current_path,
                update=update,
                context=context,
                vareon_id=pending["vareon_id"],
                user_id=query.from_user.id,
                task_id=pending["task_id"],
                target=target
            )
        )
        context.user_data.setdefault("active_tasks", {})[pending["task_id"]] = task
        
    # 🔴 FALLBACK
    else:
        logger.warning(f"[UNKNOWN MODE] mode={mode}")
        await query.edit_message_text("❌ Unknown download mode. Try again.")
        return