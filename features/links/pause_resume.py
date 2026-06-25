import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackContext
from main.state import download_status, download_tasks
from features.links.direct_link_progress import download_file_with_progress
from vareon_analytics.vr_log import log_to_db
from main.state import sessions


async def pause_download(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    download_id = context.user_data.get("current_download_id")

    if download_id and download_id in download_status:
        download_status[download_id]["paused"] = True
        task_id = context.user_data.get("task_id")
        vareon_id = sessions.get(user_id, {}).get("vareon_id", "unknown")
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            event_type="DOWNLOAD_PAUSED",
            function_name="pause_download",
            task_id=task_id,
            details={"pause": True, "resume": False},
            action_status={"status": "paused"}
        )
    else:
        await query.answer("❌ No active download to pause.", show_alert=True)


async def resume_download(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    download_id = context.user_data.get("current_download_id")

    if download_id and download_id in download_status:
        download_status[download_id]["paused"] = False
        task_id = context.user_data.get("task_id")
        vareon_id = sessions.get(user_id, {}).get("vareon_id", "unknown")
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            event_type="DOWNLOAD_RESUMED",
            function_name="resume_download",
            task_id=task_id,
            details={"pause": False, "resume": True},
            action_status={"status": "resumed"}
        )
    else:
        file_info = context.user_data.get("file_info")
        if not file_info:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="❌ No file info found. Please start a new download."
            )
            return

        message_id = query.message.message_id
        if not download_id:
            download_id = f"{user_id}_{file_info['raw_name']}"
            context.user_data["current_download_id"] = download_id

        download_status[download_id] = {"active": True, "paused": False, "downloaded": 0}

        try:
            await context.bot.edit_message_text(
                chat_id=query.message.chat_id,
                message_id=message_id,
                text=f"⬇️ Resuming download `{file_info['raw_name']}`...",
                parse_mode="Markdown"
            )
        except Exception:
            msg = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"⬇️ Resuming download `{file_info['raw_name']}`...",
                parse_mode="Markdown"
            )
            message_id = msg.message_id

        task = asyncio.create_task(
            download_file_with_progress(context, query, message_id, file_info, download_id)
        )
        download_tasks[download_id] = task

        task_id = context.user_data.get("task_id")
        vareon_id = sessions.get(user_id, {}).get("vareon_id", "unknown")
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            event_type="DOWNLOAD_RESUMED",
            function_name="resume_download",
            task_id=task_id,
            details={"pause": False, "resume": True},
            action_status={"status": "resumed"}
        )