import re
from telegram import Update
from telegram.ext import ContextTypes
from features.links.links import link_handler
from main.config import logger, ADMIN_IDS
from main.state import broadcast_mode, report_mode, report_state
from chatbot.notify import handle_notify_message, NOTIFY_PATTERN

URL_PATTERN = re.compile(r"^https?://\S+$")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip()

    if URL_PATTERN.match(text):
        await link_handler(update, context, text)
        return

    if (not broadcast_mode.get(user_id) and not report_mode.get(user_id) and not report_state.get(user_id)):
        await handle_chat_message(update, context)
      
async def handle_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip()
    logger.info(f"[CHAT_HANDLER] User {user_id} sent a non-link message: {text}")
    
    # ── Admin-only direct notify: "id:<telegram_id> <message>" ──────────────
    if user_id in ADMIN_IDS and NOTIFY_PATTERN.match(text):
            await handle_notify_message(update, context)
            return
        
    return