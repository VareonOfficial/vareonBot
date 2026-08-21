import re, sqlite3, html, asyncio
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import CallbackContext, ConversationHandler, ContextTypes
from telegram.error import RetryAfter, Forbidden, BadRequest

from main.config import ADMIN_IDS, VAREON_DB, logger
from main.state import broadcast_mode, sessions
from chatbot.broadcast.helper import load_broadcast_settings, add_message_record

#####Broadcast########
######################
# Global variable (load at bot startup)
broadcast_settings = load_broadcast_settings()

# Pacing between sends to stay under Telegram's outbound rate limit
BROADCAST_SEND_DELAY = 0.05  # seconds between each send


async def broadcast_command(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS:
        return ConversationHandler.END

    # Terminate any ongoing conversation
    context.user_data.clear()  # Clear user_data to reset conversation state
    broadcast_mode[user_id] = True

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel Broadcast", callback_data="broadcast_cancel", style="danger")]
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


async def broadcast_cancel(update: Update, context: CallbackContext):
    """
    Single cancel handler for the entire broadcast flow. Works whether
    the admin is still composing (broadcast_mode active, nothing sent yet)
    or reviewing a preview (pending_broadcast set) — decides which state
    it's cancelling based on what's currently stored, so only one
    callback_data / one registration is needed anywhere in the flow.
    """
    query = update.callback_query
    user_id = query.from_user.id
    
    had_pending = context.user_data.pop("pending_broadcast", None)

    # Remove the now-stale preview/compose message entirely rather than
    # leaving it dangling with no buttons
    try:
        await query.message.delete()
    except Exception:
        pass

    if had_pending:
        await query.message.chat.send_message(
            "❌ Broadcast preview cancelled. Send a new message whenever you're ready.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Broadcast", callback_data="broadcast_cancel", style="danger")]])
        )    
    else:
        await query.answer("Broadcast cancelled.")
        await query.message.chat.send_message("❌ Broadcast cancelled.")
        broadcast_mode.pop(user_id, None)
        
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

    VALID_STYLES = {"success", "danger", "primary"}

    for line in raw_html.splitlines():
        if "::" in line:
            row_buttons = []
            row_code_btns = []

            # Split by '|' for buttons on the SAME row
            raw_buttons = line.split("|")

            for btn_str in raw_buttons:
                if "::" in btn_str:
                    # Split into max 3 parts: text, target, optional style
                    parts = btn_str.split("::")
                    text_part = parts[0]
                    target_part = parts[1] if len(parts) > 1 else ""
                    style_part = parts[2] if len(parts) > 2 else ""

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

                    # Extract and validate optional button style
                    b_style = re.sub(r"<[^>]+>", "", style_part).strip().lower()
                    b_style = html.unescape(b_style)
                    if b_style not in VALID_STYLES:
                        b_style = None

                    # Build kwargs for InlineKeyboardButton
                    btn_kwargs = {}
                    if b_style:
                        btn_kwargs["style"] = b_style

                    style_code = f", style={b_style!r}" if b_style else ""

                    # Determine URL vs Callback Data
                    if c_target.startswith("http://") or c_target.startswith(
                        "https://"
                    ):
                        row_buttons.append(
                            InlineKeyboardButton(text=b_text, url=c_target, **btn_kwargs)
                        )
                        row_code_btns.append(
                            f"InlineKeyboardButton(text={b_text!r}, url={c_target!r}{style_code})"
                        )
                        button_info.append(
                            f"text={b_text!r}, url={c_target!r}{style_code}"
                        )
                    else:
                        row_buttons.append(
                            InlineKeyboardButton(
                                text=b_text, callback_data=c_target, **btn_kwargs
                            )
                        )
                        row_code_btns.append(
                            f"InlineKeyboardButton(text={b_text!r}, callback_data={c_target!r}{style_code})"
                        )
                        button_info.append(
                            f"text={b_text!r}, callback={c_target!r}{style_code}"
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
    """
    Fires when the admin sends broadcast content. Instead of sending to all
    users immediately, this now builds a PREVIEW and sends it back to the
    admin only, with Confirm/Cancel buttons. The real send only happens
    after the admin taps Confirm.
    """
    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS or not broadcast_mode.get(user_id):
        return

    # If a preview is already pending confirmation, don't let a stray
    # message get reparsed as new content — force admin to confirm/cancel first.
    if context.user_data.get("pending_broadcast"):
        await update.message.reply_text(
            "⚠️ You already have a broadcast pending confirmation.\n"
            "Please tap ✅ Confirm & Send or ❌ Cancel on the preview above first."
        )
        return

    # 1. Parse message data using msg_format
    data = msg_format(update, context)
    if not data:
        return

    media_type = data["media_type"]
    file_id = data["file_id"]
    text_html = data["text_html"]
    reply_markup = data["reply_markup"]

    # Generate broadcast_id now so it's stable between preview and actual send
    broadcast_id = datetime.now().strftime("%Y%m%d%H%M%S")

    # Store parsed content for the confirm step — nothing sent to users yet
    context.user_data["pending_broadcast"] = {
        "broadcast_id": broadcast_id,
        "media_type": media_type,
        "file_id": file_id,
        "text_html": text_html,
        "reply_markup": reply_markup,
    }

    # Build preview keyboard: original admin-defined buttons (if any) + Confirm/Cancel row
    preview_rows = list(reply_markup.inline_keyboard) if reply_markup else []
    preview_markup = InlineKeyboardMarkup(preview_rows)

    try:
        if media_type:
            method = getattr(context.bot, f"send_{media_type}")
            await method(
                chat_id=update.effective_chat.id,
                **{media_type: file_id},
                caption=text_html,
                parse_mode="HTML",
                reply_markup=preview_markup,
            )
        else:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text_html,
                parse_mode="HTML",
                reply_markup=preview_markup,
            )
        await update.message.reply_text(
            "👆 <b>This is a preview</b> — exactly what users will receive.\n"
            "Tap ✅ to send it to everyone, or ❌ to cancel and compose again.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Confirm & Send", callback_data="broadcast_confirm_send", style="success"),
                    InlineKeyboardButton("❌ Cancel", callback_data="broadcast_cancel", style="danger"),
                ]
            ]),
        )
    except Exception as e:
        logger.error(f"[BROADCAST_PREVIEW] Failed to build preview: {e}")
        context.user_data.pop("pending_broadcast", None)
        await update.message.reply_text(
            f"❌ Couldn't build preview (likely malformed HTML or an invalid button): {e}\n"
            f"Please fix your message and try again."
        )

async def broadcast_confirm_send(update: Update, context: CallbackContext):
    """
    Confirm at the PREVIEW stage — this is the only place that actually
    sends to all users. Includes flood-wait handling and send pacing.
    """
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    pending = context.user_data.get("pending_broadcast")
    if not pending:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⚠️ Nothing pending — this preview has expired or was already sent.")
        return

    broadcast_id = pending["broadcast_id"]
    media_type = pending["media_type"]
    file_id = pending["file_id"]
    text_html = pending["text_html"]
    reply_markup = pending["reply_markup"]

    try:
        await query.message.delete()
    except Exception:
        pass
    status_msg = await query.message.chat.send_message("📤 Sending broadcast... 0 sent so far.")

    sent_count, failed_count = 0, 0
    broadcast_settings_local = load_broadcast_settings()
    message_ids = {}  # {telegram_id: {"vareon_id": ..., "message_id": ...}} — stored as one JSON blob, not per-row

    targets = [
        target_id for target_id, prefs in broadcast_settings_local.items()
        if prefs.get("receive_updates")
    ]

    for i, target_id in enumerate(targets, start=1):
        target_chat_id = int(target_id)
        sent_msg = None

        # Retry loop: handles Telegram-mandated flood-wait backoff
        for attempt in range(3):
            try:
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
                break  # success, exit retry loop
            except RetryAfter as e:
                # Telegram explicitly told us how long to back off — honor it
                logger.warning(
                    f"[BROADCAST] Flood control hit, sleeping {e.retry_after}s "
                    f"before retrying user {target_id}"
                )
                await asyncio.sleep(e.retry_after + 0.5)
                continue
            except (Forbidden, BadRequest) as e:
                # User blocked the bot, deactivated account, etc. — not retryable
                logger.error(f"[BROADCAST] Skipping {target_id}: {e}")
                break
            except Exception as e:
                logger.error(f"[BROADCAST] Unexpected error for {target_id}: {e}")
                break

        if sent_msg:
            sent_count += 1
            vareon_id = sessions.get(target_chat_id, {}).get("vareon_id")
            message_ids[str(target_id)] = {
                "vareon_id": vareon_id,
                "message_id": sent_msg.message_id,
            }
        else:
            failed_count += 1

        # Baseline pacing between every send, success or failure
        await asyncio.sleep(BROADCAST_SEND_DELAY)

        # Periodic progress update so the admin isn't staring at a frozen message
        if i % 25 == 0 or i == len(targets):
            try:
                await status_msg.edit_text(
                    f"📤 Sending broadcast... {i}/{len(targets)} processed "
                    f"(✅ {sent_count} sent, ❌ {failed_count} failed)"
                )
            except Exception:
                pass  # edit failures (e.g. message not modified) are harmless here

    # Save ONE row for this whole broadcast, as a JSON blob
    add_message_record(broadcast_id, message_ids)

    context.user_data.pop("pending_broadcast", None)
    broadcast_mode.pop(user_id, None)

    logger.info(
        f"Broadcast {broadcast_id} completed: Sent to {sent_count}, Failed to {failed_count}"
    )
    await status_msg.edit_text(
        f"✅ <b>Broadcast sent!</b>\n\n"
        f"🆔 Broadcast ID: <code>{broadcast_id}</code>\n"
        f"📤 Sent to: {sent_count} user(s)\n"
        f"❌ Failed: {failed_count}\n\n"
        f"🗑️ To delete this broadcast for all users, run:\n"
        f"<code>/deletebroadcast {broadcast_id}</code>",
        parse_mode="HTML",
    )