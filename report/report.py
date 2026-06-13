from main.state import report_mode, report_buffer, sessions
from telegram import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Update,
)
from telegram.ext import ( CallbackContext)
from main.config import logger
from report.send_report import report_bug
from report.support_request import request_support, handle_cancel_support
from report.report_history import report_history
from utilities.cancel.cancel import cancel_process

# Shared menu content — single source of truth for both send & edit
REPORT_MENU_TEXT = "🐞 *Found a bug? Report it here.*\n\nChoose an option below:"

def get_report_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🐞 Report Bug", callback_data="report_bug"),
            InlineKeyboardButton("📜 Report History", callback_data="report_history"),
        ],
        [
            InlineKeyboardButton("💬 Request Customer Support", callback_data="report_support"),
        ],
        [
            InlineKeyboardButton("❌ Close", callback_data="report_close", style="danger"),
        ],
    ])

async def report_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    session_data = sessions.get(user_id)
    
    chat_id = update.effective_chat.id

    if not session_data:
        await context.bot.send_message(chat_id=chat_id, text="❌ Please login first using /login.")
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=REPORT_MENU_TEXT,
        reply_markup=get_report_menu_keyboard(),
        parse_mode="Markdown"
    )

async def report_buttons(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "report_bug":
        await report_bug(update, context)

    elif data == "report_history":
        await report_history(update, context)

    elif data == "report_support":
        await request_support(update, context)

    elif data == "report_cancel_support":
        await handle_cancel_support(update, context)

    elif data == "report_reopen_menu":
        await handle_reopen_report(update, context)

    elif data == "report_close":
        await cancel_process(update, context)
         
async def handle_reopen_report(update: Update, context: CallbackContext):
    """
    Navigates back to the main report menu by editing the current message in place.
    Fast — no delete/resend round trip.
    """
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text=REPORT_MENU_TEXT,
        reply_markup=get_report_menu_keyboard(),
        parse_mode="Markdown"
    )