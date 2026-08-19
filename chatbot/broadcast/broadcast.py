import re, sqlite3, html
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import CallbackContext, ConversationHandler, ContextTypes

from main.config import ADMIN_IDS, VAREON_DB, logger
from main.config import logger
from main.state import broadcast_mode
from chatbot.broadcast.helper import load_broadcast_settings, add_message_record

#####Broadcast########
######################
# Global variable (load at bot startup)
broadcast_settings = load_broadcast_settings()

async def broadcast_command(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS:
        return ConversationHandler.END
    
    # Terminate any ongoing conversation
    context.user_data.clear()  # Clear user_data to reset conversation state
    broadcast_mode[user_id] = True

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel Broadcast", callback_data="cancel_broadcast")]
    ])

    await update.message.reply_text(
        "📢 <b>Broadcast Mode Activated!</b>\n\n"
        "Send the message or media you wish to broadcast to all users.\n\n"
        "<b>1️⃣ All Media Types Supported</b>\n\n"
        "<b>2️⃣ All Text Formatting Supported</b>\n\n"
        "<b>3️⃣ Adding Inline Buttons:</b>\n"
        "Add button definitions at the bottom of your message or caption using <code>::</code>\n\n"
        "• <b>Single Button:</b>\n"
        "<code>Button Label :: callback_data</code> or <code>Support :: https://t.me/support</code>\n\n"
        "• <b>Same Row (use |):</b>\n"
        "<code>Yes :: accept | No :: decline</code>\n\n"
        "• <b>Multiple Rows (use new lines):</b>\n"
        "<code>Row 1 Button :: cb1</code>\n"
        "<code>Row 2 Button :: cb2 | Row 2 Link :: https://example.com</code>\n\n"
        "<i>Send your broadcast message now, or use the button below to cancel.</i>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    return ConversationHandler.END

async def cancel_broadcast(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if broadcast_mode.get(user_id):
        broadcast_mode.pop(user_id)
        await query.edit_message_text("❌ Broadcast cancelled.")

def msg_format(update: Update, context: CallbackContext):
    message = update.message
    if not message:
        return None

    user = message.from_user

    # 1. Determine media type and file ID
    media_type = None
    file_id = None

    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
    elif message.document:
        media_type = "document"
        file_id = message.document.file_id
    elif message.audio:
        media_type = "audio"
        file_id = message.audio.file_id
    elif message.voice:
        media_type = "voice"
        file_id = message.voice.file_id
    elif message.animation:
        media_type = "animation"
        file_id = message.animation.file_id

    # 2. Extract HTML text
    raw_html = (
        message.caption_html if media_type else message.text_html
    ) or ""

    # 3. Parse lines: separate message text from lines containing '::'
    text_lines = []
    keyboard = []
    code_rows = []
    button_info = []

    for line in raw_html.splitlines():
        if "::" in line:
            row_buttons = []
            row_code_btns = []

            # Split by '|' for buttons on the SAME row
            raw_buttons = line.split("|")

            for btn_str in raw_buttons:
                if "::" in btn_str:
                    text_part, target_part = btn_str.split("::", 1)

                    # Clean button text label
                    b_text = re.sub(r"<[^>]+>", "", text_part).strip()
                    b_text = html.unescape(b_text)

                    # Extract target / URL (checks if Telegram wrapped target in <a href="...">)
                    url_match = re.search(
                        r'href=["\']([^"\']+)["\']', target_part
                    )
                    if url_match:
                        c_target = url_match.group(1)
                    else:
                        c_target = re.sub(r"<[^>]+>", "", target_part).strip()

                    c_target = html.unescape(c_target)

                    # Determine URL vs Callback Data
                    if c_target.startswith("http://") or c_target.startswith(
                        "https://"
                    ):
                        row_buttons.append(
                            InlineKeyboardButton(text=b_text, url=c_target)
                        )
                        row_code_btns.append(
                            f"InlineKeyboardButton(text={b_text!r}, url={c_target!r})"
                        )
                        button_info.append(
                            f"text={b_text!r}, url={c_target!r}"
                        )
                    else:
                        row_buttons.append(
                            InlineKeyboardButton(
                                text=b_text, callback_data=c_target
                            )
                        )
                        row_code_btns.append(
                            f"InlineKeyboardButton(text={b_text!r}, callback_data={c_target!r})"
                        )
                        button_info.append(
                            f"text={b_text!r}, callback={c_target!r}"
                        )

            if row_buttons:
                keyboard.append(row_buttons)
                code_rows.append(f"        [{', '.join(row_code_btns)}]")
        else:
            text_lines.append(line)

    # Reconstruct text without the button lines
    clean_html = "\n".join(text_lines).strip()

    # 4. Build reply_markup and Python code string
    if keyboard:
        reply_markup = InlineKeyboardMarkup(keyboard)
        keyboard_code_str = (
            "InlineKeyboardMarkup([\n" + ",\n".join(code_rows) + "\n    ])"
        )
    else:
        reply_markup = None
        keyboard_code_str = "None"
    return {
        "user_id": user.id,
        "media_type": media_type,
        "file_id": file_id,
        "text_html": clean_html,
        "reply_markup": reply_markup,
        "button_info": button_info,
    }

async def handle_broadcast_message(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS or not broadcast_mode.get(user_id):
        return

    # 1. Parse message data using msg_format
    data = msg_format(update, context)
    if not data:
        return

    media_type = data["media_type"]
    file_id = data["file_id"]
    text_html = data["text_html"]
    reply_markup = data["reply_markup"]

    broadcast_id = datetime.now().strftime("%Y%m%d%H%M%S")
    sent_count, failed_count = 0, 0

    broadcast_settings = load_broadcast_settings()

    # 2. Iterate through users and send formatted broadcast
    for target_id, prefs in broadcast_settings.items():
        if prefs.get("receive_updates"):
            try:
                target_chat_id = int(target_id)

                # Send photo, video, document, etc. dynamically or fallback to text
                if media_type:
                    method = getattr(context.bot, f"send_{media_type}")
                    sent_msg = await method(
                        chat_id=target_chat_id,
                        **{media_type: file_id},
                        caption=text_html,
                        parse_mode="HTML",
                        reply_markup=reply_markup,
                    )
                else:
                    sent_msg = await context.bot.send_message(
                        chat_id=target_chat_id,
                        text=text_html,
                        parse_mode="HTML",
                        reply_markup=reply_markup,
                    )

                sent_count += 1

                # Save message ID for later broadcast deletion
                add_message_record(
                    broadcast_id, target_id, sent_msg.message_id
                )

            except Exception as e:
                failed_count += 1
                logger.error(f"Broadcast failed for {target_id}: {e}")

    # 3. Clear broadcast state and reply to admin
    broadcast_mode.pop(user_id, None)

    logger.info(
        f"Broadcast completed: Sent to {sent_count}, Failed to {failed_count}"
    )
    await update.message.reply_text(
        f"✅ Broadcast sent successfully!\n\n"
        f"📤 Sent to: {sent_count} user(s)\n"
        f"❌ Failed: {failed_count}\n\n"
        f"🗑️ To delete this broadcast for all users, run:\n"
        f"/deletebroadcast"
    )
