import os
import sqlite3
from main.config import VAREON_DB, logger, USERS_PATH
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackContext

from infra.broadcast import broadcast_settings, save_broadcast_settings
from main.state import sessions, report_mode
from main.dir_update import show_download_folder_menu
################################
# Settings
################################

async def settings(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id

    if report_mode.get(user_id, False):
        return
    if user_id not in sessions:
        await update.message.reply_text("❌ Please login first using /login.")
        return

    try:
        conn = sqlite3.connect(VAREON_DB)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT default_download_enabled, receive_updates
            FROM user_settings
            WHERE telegram_user_id = ?
        """, (user_id,))
        row = cursor.fetchone()

        # 🔹 If user not found → initialize
        if not row:
            default_enabled = 0
            receive_updates = 1 if user_id == 1074000261 else 0

            cursor.execute("""
                INSERT INTO user_settings (telegram_user_id, default_download_enabled, receive_updates)
                VALUES (?, ?, ?)
            """, (user_id, default_enabled, receive_updates))

            conn.commit()

        else:
            default_enabled, receive_updates = row

        conn.close()

    except Exception as e:
        logger.error(f"[SETTINGS LOAD ERROR] {e}")
        await update.message.reply_text("❌ Error loading settings.")
        return

    # 🔹 UI values
    default_status = "ON" if default_enabled else "OFF"
    updates_status = "ON" if receive_updates else "OFF"

    keyboard = []
    text = "⚙️ *Bot Settings:*"

    if user_id in sessions:
        keyboard.append([
            InlineKeyboardButton(
                f"📥 Default Download Location: {default_status}",
                callback_data="toggle_default_dl"
            )
        ])
        text += "\n\nManage your download location and bot update preferences."
    else:
        text += "\n\nYou are not logged in. Log in with /login to manage download locations."

    keyboard.append([
        InlineKeyboardButton(
            f"🤖 Receive Bot Updates: {updates_status}",
            callback_data="toggle_receive_updates"
        )
    ])

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

def load_default_settings():
    """Load user settings from DB into dict (same structure style)."""
    try:
        conn = sqlite3.connect(VAREON_DB)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT telegram_user_id, default_download_enabled
            FROM user_settings
        """)
        rows = cursor.fetchall()
        conn.close()

        data = {}
        for user_id, enabled in rows:
            data[str(user_id)] = {
                "enabled": bool(enabled)
            }

        return data

    except Exception as e:
        logger.error(f"[LOAD DEFAULT SETTINGS ERROR] {e}")
        return {}

async def handle_toggle_default_dl(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    try:
        with sqlite3.connect(VAREON_DB) as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT default_download_enabled, default_download_path
                FROM user_settings
                WHERE telegram_user_id = ?
            """, (user_id,))

            row = cursor.fetchone()

            enabled = row[0] if row else 0
            path = row[1] if row else None

            # 🔹 TURN OFF
            if enabled == 1 and path:
                cursor.execute("""
                    UPDATE user_settings
                    SET default_download_enabled = 0,
                        default_download_path = NULL
                    WHERE telegram_user_id = ?
                """, (user_id,))

                conn.commit()

                # 🔥 refresh full settings UI (safe + consistent)
                await settings(update, context)
                return

        # 🔹 TURN ON FLOW (folder selection)
        session = sessions.get(user_id)
        if not session:
            await query.edit_message_text("❌ Session expired. Please login again.")
            return

        context.user_data["setting_default_path"] = True
        context.user_data["current_mode"] = "set_default"

        base_path = f"{USERS_PATH}/{session.get('vareon_id', 0):08d}"
        context.user_data["path_stack"] = [base_path]

        await query.edit_message_text(
            "📂 Select a folder to set as your default download location."
        )
        await show_download_folder_menu(update, context)

    except Exception as e:
        logger.error(f"[TOGGLE DEFAULT DL ERROR] {e}")
        await query.answer("Error updating setting.", show_alert=True)

async def handle_toggle_receive_updates(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    try:
        conn = sqlite3.connect(VAREON_DB)
        cursor = conn.cursor()

        # 🔹 Get current value
        cursor.execute("""
            SELECT receive_updates
            FROM user_settings
            WHERE telegram_user_id = ?
        """, (user_id,))
        row = cursor.fetchone()

        if not row:
            current = 1 if user_id == 1074000261 else 0

            cursor.execute("""
                INSERT INTO user_settings (telegram_user_id, receive_updates)
                VALUES (?, ?)
            """, (user_id, current))
        else:
            current = row[0]

        # 🔹 Toggle
        new_pref = 0 if current == 1 else 1

        cursor.execute("""
            UPDATE user_settings
            SET receive_updates = ?
            WHERE telegram_user_id = ?
        """, (new_pref, user_id))

        conn.commit()
        conn.close()

    except Exception as e:
        logger.error(f"[TOGGLE UPDATES ERROR] {e}")
        await query.answer("Error updating setting.", show_alert=True)
        return

    # 🔹 UI rebuild
    updates_status = "ON" if new_pref else "OFF"

    keyboard = []
    text = "⚙️ *Bot Settings:*"

    if user_id in sessions:
        # 🔹 Fetch default_download again (for UI accuracy)
        try:
            conn = sqlite3.connect(VAREON_DB)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT default_download_enabled
                FROM user_settings
                WHERE telegram_user_id = ?
            """, (user_id,))
            row = cursor.fetchone()
            conn.close()

            default_status = "ON" if (row and row[0]) else "OFF"

        except:
            default_status = "OFF"

        keyboard.append([
            InlineKeyboardButton(
                f"📥 Default Download Location: {default_status}",
                callback_data="toggle_default_dl"
            )
        ])

        text += "\n\nManage your download location and bot update preferences."
    else:
        text += "\n\nYou are not logged in. Log in with /login to manage download locations."

    keyboard.append([
        InlineKeyboardButton(
            f"🤖 Receive Bot Updates: {updates_status}",
            callback_data="toggle_receive_updates"
        )
    ])

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )