from collections import defaultdict
import os
import shutil
import re
import asyncio
from datetime import datetime
user_locks = defaultdict(asyncio.Lock)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ConversationHandler, CallbackContext, ContextTypes)
from main.utils import ( 
    register_path,
    format_size,
    is_safe_to_delete,
)
from main.state import sessions, upload_tasks
from main.config import (RENAME, NEW_FOLDER, IST, logger, USERS_PATH)
from main.state import sessions
from features.myfiles.browse import refresh_folder_menu, ITEMS_PER_PAGE
import httpx

# Configuration (Docker-compose.yml, Docker "Container name:5000" for external connection)
API_BASE_URL = "http://link-service:5000"

async def confirm_delete(update, context, vareon_id):
    query = update.callback_query
    path = context.user_data.get("confirm_delete_path")

    if not path or not is_safe_to_delete(path, vareon_id):
        await query.edit_message_text("❌ Unsafe or unauthorized deletion attempt blocked.")
        return

    try:
        if os.path.isfile(path) or os.path.islink(path):
            os.remove(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
        else:
            await query.edit_message_text("❌ Path does not exist.")
            return
    except Exception as e:
        await query.edit_message_text(f"❌ Error deleting: {str(e)}")
        return

    context.user_data.pop("confirm_delete_path", None)
    context.user_data.pop("confirm_delete_type", None)

    path_stack = context.user_data.get("path_stack", [])
    if path_stack and path_stack[-1] == path:
        path_stack.pop()
    context.user_data["path_stack"] = path_stack

    await query.edit_message_text("✅ Deleted successfully.")
    await refresh_folder_menu(update, context, edit_text=False)
    
async def cancel_delete(update, context):
    query = update.callback_query
    await query.answer("Cancelled")
    context.user_data.pop("confirm_delete_path", None)
    context.user_data.pop("confirm_delete_type", None)
    await refresh_folder_menu(update, context, edit_text=True)
    
async def file_menu(update, context):
    query = update.callback_query
    uid = query.data.split("|")[1]
    path = context.user_data.get("path_map", {}).get(uid)

    if not path or not os.path.isfile(path):
        await query.answer("❌ File not found.", show_alert=True)
        return

    try:
        size_str = format_size(os.path.getsize(path))
    except Exception:
        size_str = "Unknown size"

    compressed_extensions = (".zip", ".rar", ".7z", ".tar", ".tar.gz", ".tar.bz2")
    is_compressed = path.lower().endswith(compressed_extensions)

    keyboard = [
        [InlineKeyboardButton("⬅️ Back", callback_data="back", style="primary")],
        [
            InlineKeyboardButton("📤 Upload", callback_data=f"upload_file|{uid}"),
            InlineKeyboardButton("✏️ Rename", callback_data=f"rename|{uid}")
        ],
        [
            InlineKeyboardButton("🚚 Move", callback_data=f"move_file|{uid}"),
            InlineKeyboardButton("🗑️ Delete", callback_data=f"delete_file|{uid}")
        ],
        [InlineKeyboardButton("🌐 Get Link", callback_data=f"get_link|{uid}")]
    ]

    if is_compressed:
        keyboard.append([
            InlineKeyboardButton("📤 Extract", callback_data=f"extract|{uid}"),
            InlineKeyboardButton("📂 Extract to Folder", callback_data=f"extract_to_folder|{uid}")
        ])
    else:
        keyboard.append([InlineKeyboardButton("🔒 Compress", callback_data=f"compress|{uid}")])

    await query.edit_message_text(
        text=f"📄 File: {os.path.basename(path)}\n📦 Size: {size_str}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def start_rename(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()

    try:
        action, uid = query.data.split("|", 1)
    except ValueError:
        await query.edit_message_text("⚠️ Invalid rename request.")
        return ConversationHandler.END

    path = context.user_data.get("path_map", {}).get(uid)
    if not path or not os.path.exists(path):
        await query.edit_message_text("❌ File or folder not found.")
        return ConversationHandler.END

    context.user_data["rename_uid"] = uid
    context.user_data["is_folder"] = action == "rename_folder"
    item_type = "folder" if action == "rename_folder" else "file"
    escaped_name = os.path.basename(path)
    await query.edit_message_text(
        f"📄 Current {item_type}: `{escaped_name}`\n\n"
        f"✏️ Enter the new name {'(without extension)' if item_type == 'folder' else '(e.g., `newfile.mp4`)'}:",
        parse_mode="Markdown"
    )
    logger.info("start_rename returning state RENAME=%s", RENAME)
    return RENAME

async def handle_rename_input(update: Update, context: CallbackContext) -> int:
    logger.info("handle_rename_input triggered | text='%s' | user=%s", update.message.text, update.message.from_user.id)
    user_id = update.message.from_user.id
    new_name = update.message.text.strip()

    # Block command-like messages
    if new_name.startswith("/"):
        await update.message.reply_text(
            "⚠️ Please enter a *valid name* like `newfolder` or `movie.mp4`, not a command.",
            parse_mode="Markdown"
        )
        return RENAME

    uid = context.user_data.get("rename_uid")
    if not uid or "path_map" not in context.user_data:
        return ConversationHandler.END

    old_path = context.user_data["path_map"].get(uid)
    if not old_path or not os.path.exists(old_path):
        await update.message.reply_text("❌ Original file or folder not found.")
        return ConversationHandler.END

    dir_path = os.path.dirname(old_path)
    old_filename = os.path.basename(old_path)
    is_folder = context.user_data.get("is_folder", False)

    # Handle file renaming (preserve extension if omitted)
    if not is_folder:
        if "." not in new_name:
            ext = os.path.splitext(old_filename)[-1]
            new_name += ext

    new_path = os.path.join(dir_path, new_name)

    if os.path.exists(new_path):
        await update.message.reply_text("⚠️ A file or folder with that name already exists. Try a different name.")
        return RENAME

    try:
        os.rename(old_path, new_path)
        context.user_data["path_map"][uid] = new_path
        item_type = "folder" if is_folder else "file"
        await update.message.reply_text(f"✅ {item_type.capitalize()} renamed successfully to `{new_name}`", parse_mode="Markdown")

        from features.myfiles.myfiles import myfiles
        await myfiles(update, context)

    except Exception as e:
        await update.message.reply_text(f"❌ Failed to rename: {str(e)}")

    return ConversationHandler.END


async def get_link(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    try:
        uid = query.data.split("|")[1]
    except (IndexError, AttributeError):
        await query.edit_message_text("❌ Invalid request data.")
        return

    path = context.user_data.get("path_map", {}).get(uid)

    if not path or not os.path.isfile(path):
        await query.edit_message_text("❌ File not found on server.")
        return

    telegram_user_id = update.effective_user.id
    vareon_id = sessions.get(telegram_user_id, {}).get("vareon_id")

    payload = {
        "file_path": path,
        "filename": os.path.basename(path),
        "telegram_user_id": telegram_user_id,
        "vareon_id": vareon_id,
        "link_sharing": "off"
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{API_BASE_URL}/internal/create-link",
                json=payload,
                timeout=10.0
            )
        
        if response.status_code != 200:
            await query.edit_message_text("❌ API failed to generate link.")
            return

        token = response.json().get("token")
        short_id = token[:12]
        
        context.user_data[f"link_{short_id}"] = token

        link = f"https://cdn-southeast-asia.vareon.top/d/{token}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel Link", callback_data=f"cancel_generated_link:{short_id}")
        ]])

        await query.edit_message_text(
            text=(
                f"🌐 **Download link generated**\n\n"
                f"🔗 [Click here to download]({link})\n\n"
                f"⚠️ *Link will stop working when cancelled.*"
            ),
            reply_markup=keyboard,
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Bot Error (get_link): {e}")
        await query.edit_message_text("❌ Server not reachable.")

async def cancel_generated_link(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    try:
        short_id = query.data.split(":", 1)[1]
    except IndexError:
        return

    token = context.user_data.get(f"link_{short_id}")

    if not token:
        await query.edit_message_text("⚠️ Link already expired or invalid.")
        return

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{API_BASE_URL}/internal/revoke-link",
                json={"token": token},
                timeout=10.0
            )

        if resp.status_code == 200:
            context.user_data.pop(f"link_{short_id}", None)
            await query.edit_message_text("✅ Link cancelled successfully.")
        else:
            await query.edit_message_text("❌ Failed to revoke link on server.")

    except Exception as e:
        logger.error(f"Bot Error (cancel_link): {e}")
        await query.edit_message_text("❌ Server not reachable.")
        
async def start_new_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = (update.effective_user.id if update.effective_user else "unknown")
    if user_id not in sessions:
        await query.message.reply_text("❌ Please login first using /login.")
        return ConversationHandler.END

    try:
        await context.bot.delete_message(query.message.chat_id, query.message.message_id)
    except Exception:
        pass  # Ignore errors if message is already deleted

    path_stack = context.user_data.get('path_stack', [])
    if not path_stack:
        await query.message.reply_text("❌ No current folder selected.")
        return ConversationHandler.END

    context.user_data['new_folder_path'] = path_stack[-1]
    await query.message.reply_text("📂 Please enter the name of the new folder:")
    logger.info("start_new_folder returning state NEW_FOLDER=%s", NEW_FOLDER)
    return NEW_FOLDER

async def handle_new_folder_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(
        "handle_new_folder_input triggered | text='%s' | user=%s",
        update.message.text,
        update.message.from_user.id
    )

    user_id = update.message.from_user.id

    if user_id not in sessions:
        await update.message.reply_text("❌ Please login first using /login.")
        return ConversationHandler.END

    folder_name = update.message.text.strip()

    # sanitize folder name
    folder_name = re.sub(r'[<>:"/\\|?*]', '', folder_name)

    if not folder_name:
        await update.message.reply_text(
            "❌ Invalid folder name. Please enter a valid name."
        )
        return NEW_FOLDER

    session_data = sessions[user_id]
    vareon_id = session_data.get("vareon_id")

    user_folder = context.user_data.get(
        "new_folder_path",
        f"{USERS_PATH}/{vareon_id}"
    )

    new_folder_path = os.path.join(user_folder, folder_name)

    try:

        # check if folder exists
        if os.path.exists(new_folder_path):

            creation_time = os.path.getctime(new_folder_path)

            formatted_time = datetime.fromtimestamp(
                creation_time,
                tz=IST
            ).strftime("%d-%m-%Y %I:%M %p")

            await update.message.reply_text(
                f"The destination already contains a folder named {folder_name}\n"
                f"Date created - {formatted_time}\n\n"
                "📂 Please enter a different folder name or cancel the process using /cancel:",
            )

            return NEW_FOLDER

        # create folder
        os.makedirs(new_folder_path)

        logger.info("Folder created: %s", new_folder_path)

        await update.message.reply_text(
            f"✅ Folder '{folder_name}' created successfully."
        )

        # ensure path_stack is valid
        path_stack = context.user_data.get("path_stack", [])

        if user_folder not in path_stack:
            path_stack.append(user_folder)

        context.user_data["path_stack"] = path_stack

        await refresh_folder_menu(update, context, edit_text=False)

    except PermissionError:

        await update.message.reply_text(
            f"❌ Permission denied: Cannot create folder '{folder_name}'."
        )

    except OSError as e:

        await update.message.reply_text(
            f"❌ OS Error: {str(e)}"
        )

    except Exception as e:

        logger.exception("Unexpected error during folder creation")

        await update.message.reply_text(
            f"❌ Unexpected error: {str(e)}"
        )

    return ConversationHandler.END

async def select_all(update, context):
    query = update.callback_query
    current_path = context.user_data.get('path_stack', [])[-1]
    current_page = context.user_data.get("current_page", 1)

    try:
        entries = list(os.scandir(current_path))
        entries.sort(key=lambda e: (not e.is_dir(), e.name.lower()))
    except Exception as e:
        logger.error(f"Cannot read folder in select_all: {e}")
        await query.edit_message_text("❌ Cannot read folder.")
        return

    total_items = len(entries)
    start = (current_page - 1) * ITEMS_PER_PAGE
    end = min(start + ITEMS_PER_PAGE, total_items)
    page_entries = entries[start:end]

    page_uids = [uid for entry in page_entries if (uid := register_path(context, entry.path))]

    if not page_uids:
        await refresh_folder_menu(update, context, edit_text=False)
        return

    selected_uids = context.user_data.get('selected_uids', set())
    page_uids_set = set(page_uids)

    if page_uids_set.issubset(selected_uids):
        selected_uids.difference_update(page_uids_set)
    else:
        selected_uids.update(page_uids_set)

    context.user_data['selected_uids'] = selected_uids
    await refresh_folder_menu(update, context, edit_text=False)

async def multi_delete(update, context):
    query = update.callback_query
    selected_uids = context.user_data.get('selected_uids', set())
    if not selected_uids:
        await query.edit_message_text("❌ No items selected.")
        return

    num_items = len(selected_uids)
    context.user_data["confirm_multi_delete_uids"] = list(selected_uids)
    context.user_data["confirm_multi_delete_count"] = num_items

    keyboard = [[
        InlineKeyboardButton("✅ Yes, delete all", callback_data="confirm_multi_delete"),
        InlineKeyboardButton("❌ No, cancel", callback_data="cancel_multi_delete")
    ]]
    await query.edit_message_text(
        f"⚠️ Delete **{num_items}** item{'s' if num_items != 1 else ''}?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def confirm_multi_delete(update, context, vareon_id):
    query = update.callback_query
    selected_uids = context.user_data.get("confirm_multi_delete_uids", [])
    if not selected_uids:
        await query.edit_message_text("❌ Session expired or no items found.")
        return

    deleted_count = 0
    errors = []

    for uid in selected_uids:
        path = context.user_data.get("path_map", {}).get(uid)
        if not path:
            errors.append(f"• {uid}: path not found")
            continue
        if not is_safe_to_delete(path, vareon_id):
            errors.append(f"• {os.path.basename(path)}: unauthorized")
            continue
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
            context.user_data["path_map"].pop(uid, None)
            deleted_count += 1
        except Exception as e:
            errors.append(f"• {os.path.basename(path)}: {str(e)}")

    context.user_data.pop("confirm_multi_delete_uids", None)
    context.user_data.pop("confirm_multi_delete_count", None)
    context.user_data.pop("multi_select_mode", None)
    context.user_data.pop("selected_uids", None)

    # Build message and truncate if too long
    if deleted_count == len(selected_uids):
        msg = f"✅ Deleted **{deleted_count}** item{'s' if deleted_count != 1 else ''}."
    else:
        msg = f"⚠️ Deleted **{deleted_count}** of **{len(selected_uids)}** items."
        if errors:
            error_text = "\n".join(errors)
            header = f"\n\nErrors ({len(errors)}):\n"
            # Telegram limit is 4096 chars, leave room for header and message
            max_error_len = 4096 - len(msg) - len(header) - 50
            if len(error_text) > max_error_len:
                error_text = error_text[:max_error_len] + "\n... (truncated)"
            msg += header + error_text

    await query.edit_message_text(msg, parse_mode="Markdown")
    context.user_data["last_action"] = "refresh"
    await refresh_folder_menu(update, context)

async def cancel_multi_delete(update, context):
    query = update.callback_query
    context.user_data.pop("confirm_multi_delete_uids", None)
    context.user_data.pop("confirm_multi_delete_count", None)
    context.user_data.pop('multi_select_mode', None)
    context.user_data.pop('selected_uids', None)
    context.user_data["last_action"] = "refresh"
    await query.edit_message_text("❌ Deletion cancelled.")
    await refresh_folder_menu(update, context)
