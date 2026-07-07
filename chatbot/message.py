import re
from telegram import Update
from telegram.ext import ContextTypes
from features.links.links import link_handler
from main.config import logger
from main.state import broadcast_mode, report_mode, report_state

URL_PATTERN = re.compile(r"^https?://\S+$")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text.strip()

    if URL_PATTERN.match(text):
        await link_handler(update, context, text)
        return
    if(not broadcast_mode.get(user_id) and not report_mode.get(user_id) and not report_state.get(user_id)):
        await handle_chat_message(update, context)

async def handle_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # placeholder for now — this is where music-by-name, future search,
    # and eventually your small AI chat layer will plug in
    logger.info(f"[CHAT_HANDLER] User {update.message.from_user.id} sent a non-link message: {update.message.text}")
    return