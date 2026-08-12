import os

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import CallbackContext, ApplicationHandlerStop

from main.config import VAREON_DB, logger, ADMIN_ID
from main.state import sessions
from middleware.agreements import _has_accepted, _record_agreement

# ════════════════════════════════════════════════════════════════════════
# POLICY — first agreement type using the engine above
# ════════════════════════════════════════════════════════════════════════

AGREEMENT_TYPE_POLICY = "policy"
CURRENT_POLICY_VERSION = "1.0"
POLICY_URL = "https://www.vareon.top/legal/privacy"
TERMS_URL = "https://www.vareon.top/legal/terms"
ENABLE_POLICY_GATE = os.getenv("ENABLE_POLICY_GATE", "true").lower() == "true"
ALLOWED_WITHOUT_POLICY = {"start", "login", "help", "logout"}


async def policy_gate(update: Update, context: CallbackContext):
    if not ENABLE_POLICY_GATE:
        return
    user = update.effective_user
    if not user:
        return  # channel posts etc. — no user to gate

    if user.id == ADMIN_ID:
        return  # admin bypass

    message = update.effective_message
    if message and message.text:
        cmd = message.text.split()[0].lstrip("/").split("@")[0]
        if cmd in ALLOWED_WITHOUT_POLICY:
            return

    if update.callback_query and update.callback_query.data == "accept_policy":
        return  # let the accept handler itself run

    vareon_id = sessions.get(user.id, {}).get("vareon_id")

    if _has_accepted(user.id, vareon_id, AGREEMENT_TYPE_POLICY, CURRENT_POLICY_VERSION):
        return  # fall through to normal handlers

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ I Accept", callback_data="accept_policy")
    ]])
    text = (
        "📋 <b>Before you continue</b>\n\n"
        "We save some usage data to personalise your experience and improve the bot. "
        "You can review or manage your saved data anytime with /mydata.\n\n"
        f'<a href="{POLICY_URL}">🔒 Privacy Policy</a> | <a href="{TERMS_URL}">📄 Terms of Use</a>\n'
        f"• Policy version: <b>{CURRENT_POLICY_VERSION}</b>\n\n"
        "Please accept to keep using the bot."
    )

    if update.callback_query:
        await update.callback_query.answer()
        await context.bot.send_message(chat_id=user.id, text=text, reply_markup=keyboard,
                                        parse_mode="HTML", disable_web_page_preview=True)
    elif message:
        await message.reply_text(text, reply_markup=keyboard,
                                  parse_mode="HTML", disable_web_page_preview=True)

    raise ApplicationHandlerStop


async def accept_policy_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    vareon_id = sessions.get(user_id, {}).get("vareon_id")

    _record_agreement(user_id, vareon_id, AGREEMENT_TYPE_POLICY, CURRENT_POLICY_VERSION)

    await query.answer("Thanks!")
    await query.edit_message_text("✅ Policy accepted. You're all set — go ahead and use the bot.")