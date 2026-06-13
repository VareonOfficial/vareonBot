from main.state import report_mode, report_buffer
from telegram import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Update,
)
from telegram.ext import ( CallbackContext)
from main.config import SUPPORT_GROUP_ID
import sqlite3
from main.config import VAREON_DB, logger

conn = sqlite3.connect(VAREON_DB, check_same_thread=False)
cursor = conn.cursor()

from datetime import datetime, timezone

async def handle_admin_reply(update: Update, context: CallbackContext):
    if update.message.chat_id != int(SUPPORT_GROUP_ID) or not update.message.reply_to_message:
        return

    replied_msg_id = update.message.reply_to_message.message_id
    
    # 1. Fetch data using the new 'status' column
    cursor.execute(
        "SELECT telegram_id, report_uid, status FROM user_reports WHERE group_msg_id = ?",
        (replied_msg_id,)
    )
    row = cursor.fetchone()

    if not row:
        return

    target_user_id, report_uid, current_status = row
    text = (update.message.text or update.message.caption or "").strip()
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # 2. Block actions if already closed/resolved
    if current_status in ["RESOLVED", "CLOSED"]:
        if text.lower().startswith(("#resolved", "#close", "#ur")):
            await update.message.reply_text(
                f"⚠️ **Action Denied:** Report `{report_uid}` is already `{current_status}`.",
                parse_mode="Markdown"
            )
            return

    # --- 🔵 UNDER REVIEW (UR) LOGIC ---
    if current_status == "PENDING" or text.lower().startswith("#ur"):
        cursor.execute(
            "UPDATE user_reports SET status = 'UR', updated_at = ? WHERE report_uid = ?",
            (now_utc, report_uid)
        )
        conn.commit()
        if text.lower().startswith("#ur"):
            await context.bot.send_message(
                chat_id=target_user_id,
                text=f"🔵 **Report Update: {report_uid}**\nYour report is now **Under Review** by our developers.",
                parse_mode="Markdown"
            )
            await update.message.reply_text(f"🔵 Status updated to Under Review: {report_uid}")
            return

    # --- ✅ RESOLVE COMMAND ---
    if text.lower().startswith("#resolved"):
        custom_msg = text[9:].strip()
        display_text = f"✅ **Report Resolved: {report_uid}**\n\n{custom_msg if custom_msg else 'This issue has been fixed.'}"

        cursor.execute(
            "UPDATE user_reports SET status = 'RESOLVED', updated_at = ? WHERE report_uid = ?",
            (now_utc, report_uid)
        )
        conn.commit()

        await context.bot.send_message(chat_id=target_user_id, text=display_text, parse_mode="Markdown")
        await update.message.reply_text(f"✅ Marked as RESOLVED: {report_uid}")
        return

    # --- 🔒 CLOSE COMMAND ---
    if text.lower().startswith("#close"):
        custom_reason = text[6:].strip() or "No further details provided."

        cursor.execute(
            "UPDATE user_reports SET status = 'CLOSED', updated_at = ? WHERE report_uid = ?",
            (now_utc, report_uid)
        )
        conn.commit()

        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"🔒 **Report Closed: {report_uid}**\n\n**Note from Team:**\n{custom_reason}",
            parse_mode="Markdown"
        )
        await update.message.reply_text(f"🔒 Report {report_uid} marked as CLOSED.")
        return

    # --- 🔁 NORMAL MESSAGE RELAY ---
    if not text and not update.message.effective_attachment:
        return

    await context.bot.copy_message(
        chat_id=target_user_id,
        from_chat_id=update.message.chat_id,
        message_id=update.message.message_id
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("↩️ Send Reply", callback_data=f"start_reply:{report_uid}")
    ]])

    await context.bot.send_message(
        chat_id=target_user_id,
        text=f"💬 **Message from Vareon Team regarding {report_uid}:**",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    admin_confirm = await update.message.reply_text(f"✅ Message sent for {report_uid}")
    cursor.execute(
        "UPDATE user_reports SET group_msg_id = ? WHERE report_uid = ?",
        (admin_confirm.message_id, report_uid)
    )
    conn.commit()
    
async def start_user_reply(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    report_uid = query.data.split(":")[1]
    
    # 1. Save the ID of the "Send Reply" button message
    context.user_data['send_reply_button_id'] = query.message.message_id
    
    report_mode[user_id] = True
    report_buffer[user_id] = []
    context.user_data['replying_to'] = report_uid

    control_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Finish Reply", callback_data="finish_user_reply"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_process")
        ]
    ])

    # 2. Capture the ID of the "Finish Reply" message
    finish_msg = await context.bot.send_message(
        chat_id=user_id,
        text=f"📝 **Reply mode activated for {report_uid}**\n\n"
             "Send your messages (text, video, photos, etc).\n"
             "When done, press **Finish Reply**.",
        parse_mode="Markdown",
        reply_markup=control_keyboard
    )
    context.user_data['finish_reply_button_id'] = finish_msg.message_id

async def finish_user_reply(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    report_uid = context.user_data.get('replying_to')
    items = report_buffer.get(user_id, [])

    if not items or not report_uid:
        await query.message.reply_text(
            "⚠️ No messages sent to reply.\n"
            "Report mode isn't cancelled yet. I am waiting for the messages to report.\n\n"
            "Press \"cancel\" button or type /cancel to cancel sending a feedback"
        )
        return

    # --- NEW: Delete BOTH button messages ---
    ids_to_delete = [
        context.user_data.get('send_reply_button_id'),
        context.user_data.get('finish_reply_button_id')
    ]
    
    for msg_id in ids_to_delete:
        if msg_id:
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=msg_id)
            except Exception as e:
                logger.error(f"Cleanup failed for msg {msg_id}: {e}")

    # --- CONTINUE with DB and Forwarding ---
    cursor.execute("SELECT group_msg_id FROM user_reports WHERE report_uid = ?", (report_uid,))
    result = cursor.fetchone()
    
    if result and result[0]:
        old_anchor_id = result[0]
        last_forwarded_id = None
        
        for msg in items:
            try:
                sent_msg = await context.bot.forward_message(
                    chat_id=SUPPORT_GROUP_ID,
                    from_chat_id=user_id,
                    message_id=msg.message_id
                )
                last_forwarded_id = sent_msg.message_id
            except Exception as e:
                logger.error(f"Forwarding failed for msg {msg.message_id}: {e}")
        try:
            await context.bot.delete_message(chat_id=SUPPORT_GROUP_ID, message_id=old_anchor_id)
        except Exception as e:
            logger.error(f"Could not delete old anchor message: {e}")

        if last_forwarded_id:
            cursor.execute("UPDATE user_reports SET group_msg_id = ? WHERE report_uid = ?", 
                           (last_forwarded_id, report_uid))
            conn.commit()

    await context.bot.send_message(
        chat_id=user_id,
        text=f"✅ Your reply for `{report_uid}` has been sent to the Vareon Development Team.",
        parse_mode="Markdown"
    )
    
    # Cleanup session data
    report_mode.pop(user_id, None)
    report_buffer.pop(user_id, None)
    context.user_data.pop('replying_to', None)
    context.user_data.pop('send_reply_button_id', None)
    context.user_data.pop('finish_reply_button_id', None)