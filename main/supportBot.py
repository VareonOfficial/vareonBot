import sqlite3
from main.config import SOS_GROUP_ID
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler
from main.config import SUPPORT_BOT_TOKEN, VAREON_DB, logger

# Database connection helper
def get_db_connection():
    conn = sqlite3.connect(VAREON_DB, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;") # Multi-bot safe mode
    return conn

# --- 1. USER TO DEV GROUP (The Forwarder) ---
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not update.message: return

    conn = get_db_connection()
    cursor = conn.cursor()

    # Check if user has an active or pending ticket
    cursor.execute("""
        SELECT ticket_msg_id, ticket_number FROM support_tickets 
        WHERE telegram_user_id = ? AND status IN ('PENDING', 'ACTIVE')
    """, (user.id,))
    ticket = cursor.fetchone()

    if not ticket:
        # If no ticket exists, we ignore or send a default message
        return

    anchor_msg_id, ticket_num = ticket

    # Prepare the sender label
    user_label = f"👤 **{user.full_name}** ({ticket_num}):\n\n"

    try:
        # If the user sent TEXT
        if update.message.text:
            new_text = f"{user_label}{update.message.text}"
            relayed_msg = await context.bot.send_message(
                chat_id=SOS_GROUP_ID,
                text=new_text,
                reply_to_message_id=anchor_msg_id,
                parse_mode="Markdown"
            )
        
        # If the user sent MEDIA (Photo, Video, Document)
        else:
            # We add the label as a caption
            caption = f"{user_label}{update.message.caption or ''}"
            relayed_msg = await update.message.copy(
                chat_id=SOS_GROUP_ID,
                caption=caption,
                reply_to_message_id=anchor_msg_id,
                parse_mode="Markdown"
            )
        
        # UPDATE the anchor to this new message so replies stay threaded
        cursor.execute("""
            UPDATE support_tickets 
            SET ticket_msg_id = ? 
            WHERE telegram_user_id = ? AND status IN ('PENDING', 'ACTIVE')
        """, (relayed_msg.message_id, user.id))
        conn.commit()

    except Exception as e:
        logger.error(f"[Support] Error relaying: {e}")
    finally:
        conn.close()

# --- 2. DEV GROUP TO USER (The Reply) ---
async def handle_dev_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Log that the bot received something in the group
    chat_id = update.effective_chat.id
    logger.info(f"[Support] Incoming group message from chat: {chat_id}")

    # 1. Basic Filters
    if chat_id != SOS_GROUP_ID: 
        logger.warning(f"[Support] Message ignored: Chat ID {chat_id} is not the Dev Group.")
        return
        
    if not update.message.reply_to_message: 
        logger.info("[Support] Message ignored: Not a reply.")
        return

    # 2. Extract IDs
    replied_to_id = update.message.reply_to_message.message_id
    dev_msg_text = update.message.text or "[Media/No Text]"
    logger.info(f"[Support] Dev is replying to message ID: {replied_to_id} | Content: {dev_msg_text[:20]}...")

    # 3. Check for #close command
    if update.message.text and "#close" in update.message.text.lower():
        logger.info(f"[Support] #close detected for msg_id {replied_to_id}")
        await close_ticket(update, context)
        return

    # 4. Database Lookup
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Check without the status filter first to see if the ticket exists at all
        cursor.execute("""
            SELECT telegram_user_id, status, ticket_number 
            FROM support_tickets 
            WHERE ticket_msg_id = ?
        """, (replied_to_id,))
        
        result = cursor.fetchone()

        if not result:
            logger.error(f"[Support] DB LOOKUP FAILED: No ticket found for ticket_msg_id {replied_to_id}")
            # Optional: Check if it's the wrong message ID by listing all pending
            cursor.execute("SELECT ticket_msg_id FROM support_tickets WHERE status != 'CLOSED'")
            active_ids = [row[0] for row in cursor.fetchall()]
            logger.info(f"[Support] Current active msg_ids in DB: {active_ids}")
            return

        user_id, status, ticket_num = result
        logger.info(f"[Support] MATCH FOUND: Ticket {ticket_num} | User {user_id} | Status {status}")

        if status == 'CLOSED':
            logger.warning(f"[Support] Attempted reply to a CLOSED ticket ({ticket_num}).")
            await update.message.reply_text("⚠️ This ticket is already closed.")
            return

        if status == 'PENDING':
            # 1. Send the "Open" notification to the user
            welcome_text = f"✅ **Ticket `{ticket_num}` is open now.**\nA developer is now reviewing your request."
            await context.bot.send_message(chat_id=user_id, text=welcome_text, parse_mode="Markdown")
            
            # 2. Update status to ACTIVE so this only happens once
            cursor.execute("UPDATE support_tickets SET status = 'ACTIVE' WHERE ticket_msg_id = ?", (replied_to_id,))
            conn.commit()
            logger.info(f"[Support] Ticket {ticket_num} moved to ACTIVE via first dev reply.")
            
        # 5. Relay to User
        try:
            await update.message.copy(chat_id=user_id)
            logger.info(f"[Support] Successfully relayed message to User {user_id}")
        except Exception as e:
            logger.error(f"[Support] FAILED to send message to User {user_id}: {e}")
            await update.message.reply_text("❌ Failed to send message to the user. They may have blocked the bot.")

    except Exception as e:
        logger.error(f"[Support] General error in handle_dev_reply: {e}")
    finally:
        conn.close()

# --- 3. THE CLOSE COMMAND ---
async def close_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    replied_to_id = update.message.reply_to_message.message_id
    cursor.execute("SELECT telegram_user_id, ticket_number FROM support_tickets WHERE ticket_msg_id = ?", (replied_to_id,))
    res = cursor.fetchone()

    if res:
        user_id, ticket_num = res
        # Update status to CLOSED
        cursor.execute("UPDATE support_tickets SET status = 'CLOSED' WHERE telegram_user_id = ?", (user_id,))
        conn.commit()

        # Notify both sides
        await context.bot.send_message(
            chat_id=user_id, 
            text=f"✅ **Ticket `{ticket_num}` Closed**\nYour support session has ended. Thank you!",
            parse_mode="Markdown"
            )
        await update.message.reply_text(f"🎫 Ticket `{ticket_num}` has been closed successfully.", parse_mode="Markdown")
    
    conn.close()

def main():
    application = ApplicationBuilder().token(SUPPORT_BOT_TOKEN).build()

    # Handler 1: Private messages from users
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_user_message))

    # Handler 2: Replies from developers in the group
    application.add_handler(MessageHandler(filters.REPLY & filters.Chat(SOS_GROUP_ID), handle_dev_reply))

    logger.info("Vareon Support Bot Started...")
    application.run_polling()

if __name__ == "__main__":
    main()