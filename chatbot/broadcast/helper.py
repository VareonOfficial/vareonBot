import sqlite3, json
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import CallbackContext

from main.config import ADMIN_IDS, VAREON_DB, logger
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

def add_message_record(broadcast_id, message_ids: dict):
    try:
        conn = sqlite3.connect(VAREON_DB)
        cursor = conn.cursor()

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
        recipients_json = json.dumps(message_ids)

        cursor.execute("""
            INSERT INTO broadcast_messages (
                broadcast_id,
                recipients,
                timestamp
            )
            VALUES (?, ?, ?)
        """, (broadcast_id, recipients_json, timestamp))

        conn.commit()
        conn.close()

    except Exception as e:
        logger.error(f"[ADD MESSAGE ERROR] {e}")

async def delete_broadcast(update: Update, context: CallbackContext):
    if update.message.from_user.id not in ADMIN_IDS:
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