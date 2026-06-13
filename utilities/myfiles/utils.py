from collections import defaultdict
import os
import asyncio
user_locks = defaultdict(asyncio.Lock)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from main.utils import register_path
from main.config import logger
from utilities.myfiles.extract import _group_volumes, _run_multi_single_loop
from utilities.myfiles.compress import compress_file


async def handle_reply_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Common function to accept the file / folder name for compress or extract
    
    USES =>
    Compress --> Needs to make a new file with a custom name
    Extract --> Needs to multi extract in a folder, so the name for the folder
    """
    if not update.message.reply_to_message:
        return

    reply_text = update.message.reply_to_message.text or ""
    user_input = update.message.text.strip()

    # ── EXTRACTION FLOW ──────────────────────────────────────
    if "Enter the name for the new extraction folder" in reply_text:
        raw_paths = context.user_data.get("multi_extract_paths")
        if not raw_paths:
            await update.message.reply_text("❌ Extraction session expired. Please try again.")
            return

        folder_name = user_input
        parent_dir = os.path.dirname(raw_paths[0])
        target_dir = os.path.join(parent_dir, folder_name)
        os.makedirs(target_dir, exist_ok=True)
        register_path(context, target_dir)

        logical_archives = _group_volumes(raw_paths)
        chat_id = update.effective_chat.id

        multi_cancel_id = context.user_data.get("multi_extract_cancel_id")
        multi_cancel_key = f"extract_cancel_{multi_cancel_id}" if multi_cancel_id else None

        status_msg = await update.message.reply_text(
            f"🚀 *Multi-Extraction Started*\nDestination: `{folder_name}`\nTotal Archives: `{len(logical_archives)}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_process|{multi_cancel_id}", style="danger")
            ]]) if multi_cancel_id else None
        )

        asyncio.create_task(
            _run_multi_single_loop(context, chat_id, logical_archives, status_msg.message_id, folder_name, target_dir, multi_cancel_id, multi_cancel_key)
        )

    # ── COMPRESSION FLOW ─────────────────────────────────────
    elif "Enter name for your" in reply_text:
        pending_key = next(
            (k for k in context.user_data if k.startswith("pending_compress_")),
            None
        )
        pending = context.user_data.get(pending_key)
        if not pending:
            await update.message.reply_text("❌ Compression session expired. Please try again.")
            return

        base_name = os.path.splitext(user_input)[0]
        format_type = pending["format_type"]
        parent_dir = pending["parent_dir"]
        file_paths = pending["file_paths"]

        extension = format_type if format_type in ["zip", "rar", "7z"] else f"tar.{format_type.split('.')[-1]}"
        compressed_path = os.path.join(parent_dir, f"{base_name}.{extension}")

        if os.path.exists(compressed_path):
            await update.message.reply_text(f"⚠️ `{os.path.basename(compressed_path)}` already exists. Use a different name.")
            return

        logger.info(f"[COMPRESS] User custom name: {base_name} | {len(file_paths)} files")

        asyncio.create_task(
            compress_file(
                context,
                update.effective_chat.id,
                file_paths if len(file_paths) > 1 else file_paths[0],
                compressed_path,
                format_type,
                base_name
            )
        )

        register_path(context, compressed_path)
        context.user_data.pop(pending_key, None)
        context.user_data.pop("compress_uid", None)
        context.user_data.pop("compress_path", None)
        context.user_data.pop("selected_uids", None)

    # ── NOT OUR REPLY ─────────────────────────────────────────
    else:
        return