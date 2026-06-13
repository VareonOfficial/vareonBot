from main.state import report_mode, report_buffer, sessions
from telegram import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Update,
)
import sqlite3
import uuid
from telegram.ext import ( CallbackContext)
from main.config import logger
from report.send_report import report_bug
from main.config import VAREON_DB, logger, SOS_GROUP_ID
def get_db_connection():
    return sqlite3.connect(VAREON_DB, check_same_thread=False)

async def request_support(update: Update, context: CallbackContext):
    query = update.callback_query
    user = update.effective_user
    user_id = user.id
    await query.answer()

    # Added ticket_id to the SELECT statement
    conn = get_db_connection() 
    cursor = conn.cursor()
    cursor.execute("""
        SELECT status, ticket_number FROM support_tickets 
        WHERE telegram_user_id = ? AND status IN ('PENDING', 'ACTIVE')
    """, (user_id,))
    existing_ticket = cursor.fetchone()

    if existing_ticket:
        status, ticket_number = existing_ticket
        
        if status == 'ACTIVE':
            active_msg = (
                "🤝 **Support Session Active**\n\n"
                "A member of our development team is already assisting you. "
                "Please continue your conversation via @VareonSupportBot.\n\n"
                "**Note:** Active sessions cannot be cancelled until resolved."
            )
            keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="report_reopen_menu", style="primary")]]
        else:
            active_msg = (
                "📩 **Ticket Already Generated**\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"🎫 **Ticket ID:** `#{ticket_number}`\n"
                "🚦 **Status:** `Waiting for Developer`\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "Your request has already been received and is in our queue. "
                "Our development team typically responds within a few hours, "
                "though it may take a bit longer depending on the volume.\n\n"
                "We appreciate your patience! You will be notified as soon as a "
                "developer picks up your case."
            )
            keyboard = [
                [InlineKeyboardButton("❌ Cancel Request", callback_data="report_cancel_support", style="danger")],
                [InlineKeyboardButton("⬅️ Back to Menu", callback_data="report_reopen_menu", style="primary")]
            ]

        await query.edit_message_text(
            text=active_msg, 
            reply_markup=InlineKeyboardMarkup(keyboard), 
            parse_mode="Markdown"
        )
        return
    
    # --- 2. GET VAREON ID  ---
    session_data = sessions.get(user_id, {})
    vareon_id = session_data.get("vareon_id")   
     
    # --- 3. GENERATE SOS MESSAGE TO GROUP ---
    ticket_num = f"VAR-{uuid.uuid4().hex[:6].upper()}"
    
    sos_text = (
        f"🆘 **NEW SUPPORT TICKET**\n\n"
        f"🎫 **Ticket:** `{ticket_num}`\n"
        f"👤 **User:** {user.full_name} (@{user.username})\n"
        f"🆔 **Telegram ID:** `{user.id}`\n"
        f"🔑 **Vareon ID:** `{vareon_id}`\n\n"
        f"💡 *Action:* Reply to this message to open the bridge."
        f"Use #close to close the Support Ticket."
    )

    try:
        group_msg = await context.bot.send_message(chat_id=SOS_GROUP_ID, text=sos_text, parse_mode="Markdown")
        ticket_msg_id = group_msg.message_id
    except Exception as e:
        logger.error(f"Failed to send SOS message: {e}")
        await query.edit_message_text("❌ Error notifying devs. Please try again later.")
        return

    # --- 4. SAVE TO DATABASE ---
    cursor.execute("""
        INSERT INTO support_tickets 
        (ticket_number, telegram_user_id, vareon_id, username, full_name, status, ticket_msg_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (ticket_num, user.id, vareon_id, user.username, user.full_name, 'PENDING', ticket_msg_id))
    conn.commit()

    # --- 5. SHOW CONFIRMATION TO USER ---
    support_info = (
        "💬 **Vareon Support Request**\n\n"
        f"🎫 **Ticket:** `{ticket_num}`\n\n"
        "Your request for assistance has been logged with our development team. "
        "To ensure your privacy and security, please note the following:\n\n"
        "⏳ **Response Time:** A member of the Vareon Team will contact you "
        "within the next few hours.\n\n"
        "🛡️ **Verified Contact:** For your safety, we will only contact you via "
        "our official support handle: @VareonSupportBot.\n\n"
        "⚠️ **Security Warning:** Our team will *never* ask for your passwords, "
        "API keys, or personal login credentials."
    )

    keyboard = [
        [InlineKeyboardButton("❌ Cancel Request", callback_data="report_cancel_support", style="danger")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="report_reopen_menu", style="primary")]
    ]
    
    await query.edit_message_text(
        text=support_info,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    
async def handle_cancel_support(update: Update, context: CallbackContext):
    """
    Deletes the ticket from DB and removes the notification message from the Dev Group.
    """
    query = update.callback_query
    user_id = update.effective_user.id
    await query.answer()

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 1. Fetch the ticket details
        cursor.execute(
            "SELECT status, ticket_msg_id FROM support_tickets WHERE telegram_user_id = ? AND status IN ('PENDING', 'ACTIVE') ORDER BY id DESC LIMIT 1", 
            (user_id,)
        )
        row = cursor.fetchone()

        if not row:
            await query.edit_message_text("❌ No active support request found.")
            return

        status, ticket_msg_id = row

        # 2. Safety Check: Don't allow cancellation if a dev is already assigned
        if status == 'ACTIVE':
            await query.edit_message_text(
                "⚠️ **Cancellation Failed**\n\n"
                "A developer has already joined this session. Please resolve it via chat.",
                parse_mode="Markdown"
            )
            return

        # 3. Attempt to delete the SOS message from the Group
        logger.info(f"[Support] Attempting delete — chat: {SOS_GROUP_ID}, msg_id: {ticket_msg_id}, type: {type(ticket_msg_id)}")
        if ticket_msg_id:
            try:
                await context.bot.delete_message(
                    chat_id=SOS_GROUP_ID, 
                    message_id=ticket_msg_id
                )
                logger.info(f"[Support] Deleted SOS message {ticket_msg_id} from group {SOS_GROUP_ID}")
            except Exception as e:
                logger.warning(f"[Support] Could not delete group message: {e}")

        cursor.execute(
            "DELETE FROM support_tickets WHERE telegram_user_id = ? AND status = 'PENDING'", 
            (user_id,)
        )
        
        if cursor.rowcount > 0:
            conn.commit()
            
            cancel_text = (
                "❌ **Support Request Cancelled**\n\n"
                "Your request has been withdrawn. The developer team has been notified."
            )
            keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="report_reopen_menu", style="primary")]]
            
            await query.edit_message_text(
                text=cancel_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        else:
            await query.answer("⚠️ Action failed. Your ticket status may have changed.", show_alert=True)

    except Exception as e:
        logger.error(f"[Support] Error during cancellation: {e}")
    finally:
        conn.close()