from main.state import report_mode, report_buffer, sessions, report_state, report_subject, report_priority
from telegram import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Update,
)
from telegram.ext import ( CallbackContext)
from main.config import SUPPORT_GROUP_ID
import json
import uuid
import sqlite3
from main.config import VAREON_DB, logger
conn = sqlite3.connect(VAREON_DB, check_same_thread=False)
cursor = conn.cursor()

async def report_bug(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    message = update.effective_message
    query = update.callback_query

    session_data = sessions.get(user_id)
    if not session_data:
        await message.reply_text("❌ Please login first using /login.")
        return

    logger.info(f"[REPORT] Activated by user_id={user_id}")
    await query.delete_message()
    
    # step 1 → ask subject
    report_state[user_id] = "subject"

    await message.reply_text(
        "📝 Enter Subject for the Report : \n\n You can use /cancel to cancel the Report."
    )


async def handle_report_subject(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    message = update.effective_message

    if report_state.get(user_id) != "subject":
        return

    subject = (message.text or "").strip()

    if not subject or len(subject) > 60:
        await message.reply_text("❌ Keep subject under 60 characters.")
        return

    report_subject[user_id] = subject
    report_state[user_id] = "priority"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Low", callback_data="priority_low", style="success"),
            InlineKeyboardButton("Medium", callback_data="priority_medium", style="primary"),
            InlineKeyboardButton("High", callback_data="priority_high", style="danger"),
        ]
    ])

    await message.reply_text(
        "Bug priority level -",
        reply_markup=keyboard
    )


async def handle_priority_selection(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id

    if report_state.get(user_id) != "priority":
        return

    await query.answer()

    priority_map = {
        "priority_low": "Low",
        "priority_medium": "Medium",
        "priority_high": "High",
    }

    report_priority[user_id] = priority_map.get(query.data)

    report_mode[user_id] = True
    report_buffer[user_id] = []

    control_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Report 🐞", callback_data="finish_report", style="success"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_process", style="danger")
        ]
    ])

    await query.message.reply_text(
        "Now send messages (text, photo, video, file, etc).\n\n"
        "When done, press *Finish bug report*.",
        parse_mode="Markdown",
        reply_markup=control_keyboard
    )

    report_state.pop(user_id, None)

async def handle_report_message(update: Update, context: CallbackContext):
    if update.callback_query: 
        return
    if not update.message:
        return

    if update.message.from_user.is_bot:
        return

    user_id = update.message.from_user.id
    
    if not report_mode.get(user_id):
        return

    msg = update.message

    report_buffer.setdefault(user_id, []).append(msg)

    logger.info(
        f"[REPORT] Captured | user_id={user_id} "
        f"| message_id={msg.message_id} "
        f"| type={msg.effective_attachment or 'text'} "
        f"| total={len(report_buffer[user_id])}"
    )

async def finish_report(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    user_id = user.id
    message = query.message

    # Safety check: Is the user actually in report mode?
    if not report_mode.get(user_id):
        return

    # Check if they actually sent messages
    items = report_buffer.get(user_id, [])
    if not items:
        await message.reply_text("⚠️ No report messages were sent. Process cancelled.")
        return

    # 1. Prepare Metadata & IDs
    summary_counts = {}
    for msg in items:
        key = "text" if msg.text else "photo" if msg.photo else "video" if msg.video else "document" if msg.document else "other"
        summary_counts[key] = summary_counts.get(key, 0) + 1

    session_data = sessions.get(user_id, {})
    vareon_id = session_data.get("vareon_id", "Unknown")
    subject = report_subject.get(user_id)
    priority = report_priority.get(user_id)
    
    # Generate UID before inserting so we have it for the Support Group header
    report_uid = f"VAR-{uuid.uuid4().hex[:6].upper()}"
    report_metadata = json.dumps({
        "subject": subject,
        "summary": summary_counts
    })

    # 2. Database Insert (New Schema)
    try:
        cursor.execute("""
            INSERT INTO user_reports 
            (report_uid, telegram_id, vareon_id, username, full_name, total_messages, message_summary, priority, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            report_uid,
            user_id,
            vareon_id,
            user.username,
            user.full_name,
            len(items),
            report_metadata,
            priority,   # <--- Added this
            'PENDING'
        ))
        conn.commit()
        
        # Capture the actual row ID for the next update
        row_id = cursor.lastrowid
        logger.info(f"[Database] New report created: {report_uid} (Row: {row_id})")
        
    except Exception as e:
        logger.error(f"[Database] Insert Error: {e}")
        conn.rollback()
        await message.reply_text("❌ Database error. Please try again later.")
        return

    # 3. Send Header to Support Group
    clickable_user = f'<a href="tg://user?id={user_id}">{user_id}</a>'
    header = (
        f"📩 <b>New 🐞 report: {report_uid}</b>\n\n"
        f"<b>Vareon ID:</b> {vareon_id}\n"
        f"<b>User ID:</b> {clickable_user}\n"
        f"<b>Name:</b> {user.full_name or 'Unknown'}\n"
        f"<b>Summary:</b> {subject or 'N/A'}\n"
        f"<b>Priority:</b> {priority or 'N/A'}"
    )

    try:
        header_msg = await context.bot.send_message(
            chat_id=SUPPORT_GROUP_ID, 
            text=header, 
            parse_mode="HTML"
        )

        # 4. Update row with the Group Message ID and fetch the timestamp
        cursor.execute(
            "UPDATE user_reports SET group_msg_id = ? WHERE id = ?", 
            (header_msg.message_id, row_id)
        )
        
        cursor.execute("SELECT created_at FROM user_reports WHERE id = ?", (row_id,))
        db_row = cursor.fetchone()
        created_at = db_row[0] if db_row else "Just now"
        conn.commit()

    except Exception as e:
        logger.error(f"Error updating support group info: {e}")
        created_at = "Pending"

    # 5. Forward messages to the Support Group
    for msg in items:
        try:
            await context.bot.forward_message(
                chat_id=SUPPORT_GROUP_ID, 
                from_chat_id=user_id, 
                message_id=msg.message_id
            )
        except Exception as e:
            logger.error(f"Forward failed for message {msg.message_id}: {e}")

    # 6. Success Reply to User
    await message.reply_text(
        f"✅ **Reported successfully**\n\n"
        f"🆔 **Bug ID:** `{report_uid}`\n"
        f"🕒 **Created at:** `{created_at} UTC`\n\n"
        f"📌 You will be notified here when the status changes.\n"
        f"Thank you!",
        parse_mode="Markdown"
    )

    # 7. Cleanup session data
    report_mode.pop(user_id, None)
    report_buffer.pop(user_id, None)
    report_subject.pop(user_id, None)
    report_priority.pop(user_id, None)

    logger.info(f"[REPORT] Process complete for {report_uid} | user_id={user_id}")