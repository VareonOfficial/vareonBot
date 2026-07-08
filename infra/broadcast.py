import json
import os, sqlite3
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import CallbackContext, ConversationHandler

from main.config import ADMIN_ID, VAREON_DB, logger
from main.config import logger
from main.state import broadcast_mode, report_mode, report_state

#####Broadcast########
######################

def save_broadcast_settings(data):
    """Save all broadcast settings to SQLite."""
    try:
        conn = sqlite3.connect(VAREON_DB)
        cursor = conn.cursor()

        for user_id, settings in data.items():
            try:
                user_id = int(user_id)
                receive_updates = 1 if settings.get("receive_updates") else 0

                cursor.execute("""
                    INSERT INTO user_settings (telegram_user_id, receive_updates)
                    VALUES (?, ?)
                    ON CONFLICT(telegram_user_id) DO UPDATE SET
                        receive_updates=excluded.receive_updates
                """, (user_id, receive_updates))

            except Exception as inner_e:
                logger.error(f"[BROADCAST SAVE ERROR] user_id={user_id} | {inner_e}")

        conn.commit()
        conn.close()

    except Exception as e:
        logger.error(f"[BROADCAST SAVE ERROR] {e}")
        
def load_broadcast_settings() -> dict:
    """Load broadcast settings from SQLite at startup."""
    try:
        conn = sqlite3.connect(VAREON_DB)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT telegram_user_id 
            FROM user_settings 
            WHERE receive_updates = 1
        """)
        
        rows = cursor.fetchall()
        conn.close()

        data = {}
        for (user_id,) in rows:
            data[str(user_id)] = {"receive_updates": True}

        logger.info(f"[BROADCAST] Loaded {len(data)} users for broadcast at startup.")
        return data

    except Exception as e:
        logger.error(f"[BROADCAST LOAD ERROR] {e}")
        return {}


# Global variable (load at bot startup)
broadcast_settings = load_broadcast_settings()

async def broadcast_command(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if user_id != ADMIN_ID:
        return ConversationHandler.END
    
    # Terminate any ongoing conversation
    context.user_data.clear()  # Clear user_data to reset conversation state
    broadcast_mode[user_id] = True

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel Broadcast", callback_data="cancel_broadcast")]
    ])

    await update.message.reply_text(
        "📣 *Broadcast Mode Activated!*\n\nSend the message you want to broadcast.\n"
        "_You can send text, photos, videos, gifs, or files._",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    return ConversationHandler.END

async def cancel_broadcast(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if broadcast_mode.get(user_id):
        broadcast_mode.pop(user_id)
        await query.edit_message_text("❌ Broadcast cancelled.")

async def handle_broadcast_message(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if user_id != ADMIN_ID or not broadcast_mode.get(user_id):
        return
    if report_mode.get(user_id): return
    if report_state.get(user_id): return
    broadcast_id = datetime.now().strftime("%Y%m%d%H%M%S")
    message = update.message
    sent_count, failed_count = 0, 0
    
    broadcast_settings = load_broadcast_settings()
    for target_id, prefs in broadcast_settings.items():
        if prefs.get("receive_updates"):
            try:
                # Try to copy the message directly (works for most types)
                sent_msg = await message.copy(chat_id=int(target_id))
                sent_count += 1

                # ✅ Save the message ID for later deletion
                add_message_record(broadcast_id, target_id, sent_msg.message_id)

            except Exception as e:
                logger.warning(f"Copy failed for {target_id}: {e}")

                # Fallback: detect type and send explicitly
                try:
                    if message.document:
                        sent_msg = await context.bot.send_document(
                            chat_id=int(target_id),
                            document=message.document.file_id,
                            caption=message.caption or ""
                        )
                    elif message.photo:
                        sent_msg = await context.bot.send_photo(
                            chat_id=int(target_id),
                            photo=message.photo[-1].file_id,
                            caption=message.caption or ""
                        )
                    elif message.video:
                        sent_msg = await context.bot.send_video(
                            chat_id=int(target_id),
                            video=message.video.file_id,
                            caption=message.caption or ""
                        )
                    elif message.audio:
                        sent_msg = await context.bot.send_audio(
                            chat_id=int(target_id),
                            audio=message.audio.file_id,
                            caption=message.caption or ""
                        )
                    else:
                        # As a last resort, try sending text
                        if message.text:
                            sent_msg = await context.bot.send_message(
                                chat_id=int(target_id),
                                text=message.text
                            )
                        else:
                            logger.error(f"Unsupported message type for broadcast to {target_id}")
                            failed_count += 1
                            continue

                    sent_count += 1

                    # ✅ Save the message ID for later deletion
                    add_message_record(broadcast_id, target_id, sent_msg.message_id)

                except Exception as e2:
                    failed_count += 1
                    logger.error(f"Broadcast failed to {target_id}: {e2}")

    # ✅ Clear broadcast mode only after forwarding
    broadcast_mode.pop(user_id, None)

    logger.info(f"Broadcast completed: Sent to {sent_count}, Failed to {failed_count}")
    await update.message.reply_text(
        f"✅ Broadcast sent successfully!\n\n"
        f"📤 Sent to: {sent_count} user(s)\n"
        f"❌ Failed: {failed_count}\n\n"
        f"🗑️ To delete this broadcast for all users, run:\n"
        f"/deletebroadcast"
    )

def add_message_record(broadcast_id, chat_id, message_id):
    try:
        conn = sqlite3.connect(VAREON_DB)
        cursor = conn.cursor()

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

        cursor.execute("""
            INSERT INTO broadcast_messages (
                broadcast_id,
                telegram_user_id,
                message_id,
                timestamp
            )
            VALUES (?, ?, ?, ?)
        """, (broadcast_id, chat_id, message_id, timestamp))

        conn.commit()
        conn.close()

    except Exception as e:
        logger.error(f"[ADD MESSAGE ERROR] {e}")

async def delete_broadcast(update: Update, context: CallbackContext):
    if update.message.from_user.id != ADMIN_ID:
        return

    try:
        conn = sqlite3.connect(VAREON_DB)
        cursor = conn.cursor()

        # 🔹 Get latest broadcast_id
        cursor.execute("""
            SELECT broadcast_id
            FROM broadcast_messages
            ORDER BY broadcast_id DESC
            LIMIT 1
        """)
        row = cursor.fetchone()

        if not row:
            await update.message.reply_text("⚠️ No broadcasts to delete.")
            conn.close()
            return

        last_broadcast_id = row[0]

        # 🔹 Get all messages of that broadcast
        cursor.execute("""
            SELECT telegram_user_id, message_id
            FROM broadcast_messages
            WHERE broadcast_id = ?
        """, (last_broadcast_id,))

        records = cursor.fetchall()

        deleted, failed = 0, 0

        for chat_id, message_id in records:
            try:
                await context.bot.delete_message(
                    chat_id=int(chat_id),
                    message_id=int(message_id)
                )
                deleted += 1
            except Exception as e:
                failed += 1
                logger.error(f"Delete failed for {chat_id}: {e}")

        # 🔹 Delete from DB
        cursor.execute("""
            DELETE FROM broadcast_messages
            WHERE broadcast_id = ?
        """, (last_broadcast_id,))

        conn.commit()
        conn.close()

        await update.message.reply_text(
            f"🗑 Deleted last broadcast\n"
            f"✅ Deleted: {deleted}\n"
            f"❌ Failed: {failed}"
        )

    except Exception as e:
        logger.error(f"[DELETE BROADCAST ERROR] {e}")
        await update.message.reply_text("❌ Error deleting broadcast.")