from collections import defaultdict
import os
from pathlib import Path
import shutil
import asyncio
user_locks = defaultdict(asyncio.Lock)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ConversationHandler, CallbackContext, ContextTypes)
from main.utils import is_safe_to_delete
from main.state import sessions, report_mode
from utilities.myfiles.browse import refresh_folder_menu
from utilities.myfiles.select_actions import (confirm_delete, cancel_delete, select_all, multi_delete, confirm_multi_delete, 
cancel_multi_delete, file_menu, get_link)
from utilities.myfiles.compress import compress,  compress_format
from utilities.myfiles.extract import (show_extraction_folder_menu, extract_archive, multi_extract, handle_extract_multi_sep,
prompt_single_folder_name)
from utilities.myfiles.move import multi_move, show_move_folder_menu
from utilities.myfiles.upload import run_tdl_upload
from main.config import (logger, USERS_PATH, TELEGRAM_MAX_FILE_SIZE)
from main.state import sessions

################################
# Myfiles setup here
################################

async def myfiles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = None
    chat = None

    if update.message:
        user = update.message.from_user
        chat = update.message.chat
    elif update.callback_query:
        user = update.callback_query.from_user
        chat = update.callback_query.message.chat

    if user is None or chat is None:
        logger.error("User or chat not found in update during myfiles()")
        return

    user_id = user.id

    if user_id not in sessions:
        await context.bot.send_message(
            chat_id=chat.id,
            text="❌ Please login first using /login."
        )
        return

    if report_mode.get(user_id, False):
        return

    session_data = sessions[user_id]
    vareon_id = session_data.get("vareon_id")

    user_folder = f"{USERS_PATH}/{vareon_id}"

    context.user_data.pop('multi_select_mode', None)
    context.user_data.pop('selected_uids', None)
    context.user_data["path_stack"] = [user_folder]
    context.user_data["current_page"] = 1
    context.user_data["selected_uids"] = set()
    context.user_data["mode"] = "normal"

    try:
        await refresh_folder_menu(update, context, edit_text=False)

    except Exception as e:
        logger.error("Error initializing myfiles menu: %s", e)
        await context.bot.send_message(
            chat_id=chat.id,
            text="❌ Failed to load your files."
        )

async def myfiles_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # split action and uid if present
    if "|" in data:
        parts = data.split("|", 2)
        action = parts[0]
        uid = parts[1] if len(parts) > 1 else None
    else:
        action = data
        uid = None

    user_id = (update.effective_user.id if update.effective_user else "unknown")
    if user_id not in sessions:
        await query.edit_message_text("❌ Please login first using /login.")
        return
    logger.info(
        "myfiles_callback invoked | user=%s | data='%s' | chat_id=%s | message_id=%s",
        user_id,
        data,
        query.message.chat_id,
        query.message.message_id
    )
    path = None
    if uid:
        if action not in ["multi_exec", "select"]:
            path = context.user_data.get("path_map", {}).get(uid)
            if not path or not os.path.exists(path):
                await query.edit_message_text("❌ Path not found.")
                return
    session_data = sessions[user_id] 
    vareon_id = session_data.get("vareon_id")
    main_directory = f"{USERS_PATH}/{vareon_id}"

    if action =="noop":
        logger.debug("noop callback - ignored")
        return

    if action == "open":
        context.user_data['path_stack'].append(path)
        context.user_data["last_action"] = "refresh"
        await refresh_folder_menu(update, context)
        return
    
    elif action =="back":
        logger.info("Handling 'back'")
        path_stack = context.user_data.get('path_stack', [])
        if len(path_stack) > 1:
            path_stack.pop()
        current_path = path_stack[-1] if path_stack else main_directory
        context.user_data['path_stack'] = path_stack
        context.user_data["last_action"] = "refresh"
        await refresh_folder_menu(update, context)
        return
    
    elif action =="refresh":
        logger.info("Handling 'refresh'")
        context.user_data["last_action"] = "refresh"
        await refresh_folder_menu(update, context)
        return
    elif action == "confirm_delete":
        await confirm_delete(update, context, vareon_id)
        return

    elif action == "cancel_delete":
        await cancel_delete(update, context)
        return
    
    elif action == "extract_nav":
        _, uid = data.split("|", 1)
        path = context.user_data.get("path_map", {}).get(uid)
        if not path or not os.path.isdir(path):
            await query.edit_message_text("❌ Folder not found.")
            return
        context.user_data["move_path_stack"].append(path)
        await show_extraction_folder_menu(update, context)
        return

    elif action == "extract_nav_back":
        stack = context.user_data.get("move_path_stack", [])
        if len(stack) > 1:
            stack.pop()
            await show_extraction_folder_menu(update, context)
        else:
            await refresh_folder_menu(update, context)

    elif action == "extract_execute":
        uid = context.user_data.get("pending_extract_uid")
        extract_mode = context.user_data.get("extract_mode", "flat")
        if not uid:
            await query.edit_message_text("❌ No file selected for extraction.")
            return

        archive_path = context.user_data.get("path_map", {}).get(uid)
        target_dir = context.user_data["move_path_stack"][-1]

        if extract_mode == "folder":
            base_name = Path(archive_path).stem
            if base_name.endswith(".tar"):
                base_name = Path(base_name).stem
            target_dir = os.path.join(target_dir, base_name)

        os.makedirs(target_dir, exist_ok=True)
        asyncio.create_task(
            extract_archive(
                context, 
                query.message.chat_id, 
                archive_path, 
                target_dir, 
                extract_mode=extract_mode,
                existing_msg_id=query.message.message_id # <--- REUSE MENU MESSAGE
            )
        )
        return

    elif action == "move_nav":
        _, uid = data.split("|", 1)
        path = context.user_data.get("path_map", {}).get(uid)
        if not path or not os.path.isdir(path):
            await query.edit_message_text("❌ Folder not found.")
            return
        context.user_data["move_path_stack"].append(path)
        await show_move_folder_menu(update, context)
        return

    elif action =="move_nav_back":
        if len(context.user_data.get("move_path_stack", [])) > 1:
            context.user_data["move_path_stack"].pop()
        await show_move_folder_menu(update, context)
        return

    elif action =="move_execute":
        source_path = context.user_data.get("move_file_path")
        dest_path = context.user_data["move_path_stack"][-1]
        if not source_path or not os.path.exists(source_path):
            await query.edit_message_text("❌ Source file not found.")
            return
        try:
            shutil.move(source_path, dest_path)
            await query.edit_message_text("✅ File moved successfully.")
        except Exception as e:
            await query.edit_message_text(f"❌ Move failed: {e}")
        return
    
    elif action =="multi_select":
        context.user_data['multi_select_mode'] = True
        context.user_data['selected_uids'] = set()
        context.user_data["last_action"] = "refresh"
        await refresh_folder_menu(update, context)
        return

    elif action == "select":
        _, uid = data.split("|", 1)
        selected_uids = context.user_data.get('selected_uids', set())
        if uid in selected_uids:
            selected_uids.remove(uid)
        else:
            selected_uids.add(uid)
        context.user_data['selected_uids'] = selected_uids
        await refresh_folder_menu(update, context, edit_text=False)
        return

    elif action == "select_all":
        await select_all(update, context)
        return

    elif action =="multi_cancel":
        context.user_data.pop('multi_select_mode', None)
        context.user_data.pop('selected_uids', None)
        await refresh_folder_menu(update, context)
        return
    
    elif action == "multi_delete":
        await multi_delete(update, context)
    elif action == "confirm_multi_delete":
        await confirm_multi_delete(update, context, vareon_id)
    elif action == "cancel_multi_delete":
        await cancel_multi_delete(update, context)
    elif action == "multi_move":
        await multi_move(update, context, vareon_id)        
    elif action == "file":
        await file_menu(update, context)
    elif action == "get_link":
        await get_link(update, context)
        
    elif action == "upload_file":
        current_path = context.user_data.get("path_stack", [])[-1]
        file_name = os.path.basename(path)
        
        # Calculate file size in bytes
        file_size = os.path.getsize(path)

        if file_size > TELEGRAM_MAX_FILE_SIZE:
            await query.message.reply_text(
                "⚠️ **File too large**\n\n"
                "Uploads above 2GB are not yet supported on the free plan of VAREON. "
                "These features will be added and billed later for premium users.",
                parse_mode="Markdown"
            )
            return

        progress_msg = await query.message.reply_text(
            f"📤 Preparing upload → `{file_name}`\n"
            f"📂 Location: `{current_path}`",
            parse_mode="Markdown"
        )     
        task = asyncio.create_task(
            run_tdl_upload(
                progress_msg=progress_msg,
                path=current_path,
                file_name=file_name,
                context=context,
                user_id=user_id,
                vareon_id=vareon_id,
            )
        )
        context.user_data["active_tdl_task"] = task
        
    elif action == "move_file":
        if not os.path.isfile(path):
            await query.edit_message_text("❌ File not found.")
            return
        context.user_data["move_file_uid"] = uid
        context.user_data["move_file_path"] = path
        context.user_data["current_mode"] = "move_file"
        context.user_data["move_path_stack"] = [main_directory]

        await query.edit_message_text(
            f"📄 Moving file: {os.path.basename(path)}\n\nSelect a target folder to move to:"
        )
        await show_move_folder_menu(update, context)
        return

    elif action == "compress":
        await compress(update, context)
        
    elif action == "multi_compress":
        await compress(update, context)
        
    elif action in ["compress_format", "multi_exec"]:
        await compress_format(update, context, main_directory)

    elif action == "cancel_compress":
        await refresh_folder_menu(update, context)
        return

    elif action == "extract":
        context.user_data["pending_extract_uid"] = uid
        context.user_data["extract_mode"] = "flat"
        context.user_data["move_path_stack"] = [main_directory]

        await query.edit_message_text("📂 Select a folder to extract into (or extract here if none):")
        await show_extraction_folder_menu(update, context)
        return
     
    elif action == "multi_extract":
        await multi_extract(update, context) 
    elif action == "extract_multi_sep":
        await handle_extract_multi_sep(update, context)

    elif action == "extract_multi_single_prompt":
        await prompt_single_folder_name(update, context)
    
    elif action == "extract_to_folder":
        context.user_data["pending_extract_uid"] = uid
        context.user_data["extract_mode"] = "folder"
        context.user_data["move_path_stack"] = [main_directory]

        await query.edit_message_text("📂 Select a folder to extract into (or extract here if none):")
        await show_extraction_folder_menu(update, context)
        return

    elif action == "extract_to_folder":
        if not os.path.isfile(path):
            await query.edit_message_text("❌ File not found.")
            return
        parent_dir = os.path.dirname(path)
        if not is_safe_to_delete(parent_dir, vareon_id):
            await query.edit_message_text("❌ Cannot extract to unauthorized location.")
            return
        base_name = os.path.splitext(os.path.basename(path))[0]
        extraction_dir = os.path.join(parent_dir, base_name)
        context.user_data["extract_uid"] = uid
        context.user_data["extract_mode"] = "folder"
        context.user_data["extract_dir"] = extraction_dir
        if os.path.exists(extraction_dir):
            keyboard = [
                [InlineKeyboardButton("✅ Yes, overwrite", callback_data=f"overwrite_extract|{uid}"),
                 InlineKeyboardButton("❌ No, skip", callback_data=f"skip_extract|{uid}")]
            ]
            await query.edit_message_text(
                text=f"⚠️ Folder `{base_name}` already exists. Do you want to overwrite it?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await extract_archive(context, query.message.chat_id, path, extraction_dir)
        return

    elif action == "overwrite_extract":
        if not os.path.exists(path):
            await query.edit_message_text("❌ File not found.")
            return
        mode = context.user_data.get("extract_mode")
        if mode == "parent":
            extraction_dir = os.path.dirname(path)
        elif mode == "folder":
            extraction_dir = context.user_data.get("extract_dir")
        else:
            await query.edit_message_text("❌ Invalid extraction mode.")
            return
        if not is_safe_to_delete(extraction_dir, vareon_id):
            await query.edit_message_text("❌ Cannot extract to unauthorized location.")
            return
        if mode == "folder" and os.path.exists(extraction_dir):
            shutil.rmtree(extraction_dir)
        await extract_archive(context, query.message.chat_id, path, extraction_dir, overwrite=True)
        context.user_data.pop("extract_conflict", None)
        context.user_data.pop("extract_dir", None)
        return

    elif action == "skip_extract":
        context.user_data.pop("extract_conflict", None)
        context.user_data.pop("extract_dir", None)
        await query.edit_message_text("❌ Extraction cancelled.")
        return

    elif action in ["delete_file", "delete_folder"]:
        confirm_type = "file" if action == "delete_file" else "folder"
        context.user_data["confirm_delete_path"] = path
        context.user_data["confirm_delete_type"] = confirm_type
        filename = os.path.basename(path)
        keyboard = [
            [InlineKeyboardButton("✅ Yes, delete", callback_data="confirm_delete"),
             InlineKeyboardButton("❌ No, go back", callback_data="cancel_delete")]
        ]
        await query.edit_message_text(
            text=f"⚠️ Do you really want to delete `{filename}`?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    else:
        logger.warning("Unhandled callback data: %s", data)
