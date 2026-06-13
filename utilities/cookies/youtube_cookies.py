import os
import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from main.state import sessions, awaiting_cookie
from main.config import logger, YT_COOKIE_VIDEO_ID, COOKIES_PATH
from pathlib import Path
################################
# 🔘 Set YouTube cookies
################################

async def set_youtube_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    try:
        await query.message.delete()
    except Exception as e:
        logger.warning(f"Could not delete original message: {e}")

    user_id = query.from_user.id
    awaiting_cookie[user_id] = "youtube"

    await query.message.reply_text(
        "📥 To enable YouTube downloads, please follow these steps to provide your cookies data securely:\n\n"
        "<b>Step-by-Step Guide:</b>\n\n"
        "1. <b>Install</b> the <b>\"cookies.txt\" extension</b> on your laptop or desktop browser.\n"
        "👉 <a href=\"https://chromewebstore.google.com/detail/cclelndahbckbenkjhflpdbgdldlbecc?utm_source=item-share-cb\">Click here to install from Chrome Web Store</a>\n"
        "Then click <b>\"Add Extension\"</b>.\n\n"
        "2. Open <a href=\"https://youtube.com\">youtube.com</a> and <b>log in</b> using your YouTube credentials.\n\n"
        "3. Click the <b>cookies.txt extension icon</b> in your browser toolbar, look for the <b>\"Export Format:\"</b> option and set it to <b>NETSCAPE</b>. Then click on the <b>\"Export\"</b> button to save it as a NETSCAPE file.\n\n"
        "4. <b>Upload</b> the NETSCAPE file into <i>this bot chat</i>.\n\n"
        "🔐 <b>Security Notice:</b>\n"
        "<blockquote>"
        "By submitting your cookies, you confirm that you understand and accept <b>Vareon’s Terms of Service and Privacy Policy</b>.\n\n"
        "Vareon does <b>not store passwords</b> and is <b>not liable</b> for any unauthorized access, account misuse, or legal consequences resulting from your activity.\n\n"
        "⚠️ Always use this feature responsibly and only with accounts you <i>personally own</i>."
        "</blockquote>",
        parse_mode="HTML",
        link_preview_options={"is_disabled": True}
    )
    await query.message.reply_video(
        video=YT_COOKIE_VIDEO_ID,
        caption="Here’s a quick video guide showing how to export and upload your YouTube cookies."
    )
    await query.message.reply_text(
        "Use /cancel to cancel this operation if you change your mind."
    )

################################
# 🔘 DISCONNECT YOUTUBE CALLBACK
################################
async def remove_youtube_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    user_id = update.effective_user.id
    session_data = sessions.get(user_id)

    if not session_data:
        return

    vareon_id = session_data.get("vareon_id")
    if not vareon_id:
        return
    
    file_path = COOKIES_PATH / "youtube" / f"{vareon_id}.txt"

    if os.path.exists(file_path):
        os.remove(file_path)

    from utilities.cookies.set_cookies import cookies
    await cookies(update, context)