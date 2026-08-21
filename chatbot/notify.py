import re, sqlite3, asyncio
from telegram import Update
from telegram.ext import CallbackContext
from telegram.error import RetryAfter, Forbidden, BadRequest

from main.config import ADMIN_IDS, VAREON_DB, logger
from chatbot.broadcast.broadcast import msg_format

# Regex updated to match: tg:<id> or vareon:<id>
NOTIFY_PATTERN = re.compile(r"^(tg|vareon):(\S+)", re.IGNORECASE)
NOTIFY_SEND_DELAY = 0.05


def resolve_target_ids(target_kind: str, target_value: str) -> list[int]:
    """Resolves a tg: or vareon: reference to one or more Telegram IDs."""
    if target_kind == "tg":
        try:
            return [int(target_value)]
        except ValueError:
            return []

    try:
        conn = sqlite3.connect(VAREON_DB)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT telegram_user_id FROM telegram_auth WHERE vareon_id = ?",
            (target_value,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [int(r[0]) for r in rows]
    except Exception as e:
        logger.error(f"[NOTIFY] Failed to resolve vareon_id {target_value}: {e}")
        return []


async def handle_notify_message(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS:
        return

    message = update.message
    raw_first_line = (message.caption if message.caption else message.text) or ""
    first_line = raw_first_line.strip().splitlines()[0] if raw_first_line.strip() else ""

    match = NOTIFY_PATTERN.match(first_line)
    if not match:
        await message.reply_text(
            "⚠️ First line must start with <code>tg:&lt;telegram_id&gt;</code> "
            "or <code>vareon:&lt;vareon_id&gt;</code>",
            parse_mode="HTML",
        )
        return

    target_kind, target_value = match.group(1).lower(), match.group(2)
    target_ids = resolve_target_ids(target_kind, target_value)

    if not target_ids:
        await message.reply_text(f"⚠️ No Telegram account found for {target_kind}:{target_value}")
        return

    # Reuse msg_format entirely — handles media/HTML/button parsing
    data = msg_format(update, context)
    if not data:
        return

    text_html = data["text_html"]    
    # Removes "tg:123" or "vareon:abc" plus any trailing spaces/newlines at the start
    text_html = re.sub(r"^(tg|vareon):\S+\s*", "", text_html, flags=re.IGNORECASE)
    
    data["text_html"] = text_html
    if not data["text_html"] and not data["media_type"]:
        await message.reply_text("⚠️ Cannot send an empty message.")
        return

    sent_count, failed_count = 0, 0
    for target_chat_id in target_ids:
        sent_msg = None
        for attempt in range(3):
            try:
                if data["media_type"]:
                    method = getattr(context.bot, f"send_{data['media_type']}")
                    sent_msg = await method(
                        chat_id=target_chat_id,
                        **{data["media_type"]: data["file_id"]},
                        caption=data["text_html"],
                        parse_mode="HTML",
                        reply_markup=data["reply_markup"],
                    )
                else:
                    sent_msg = await context.bot.send_message(
                        chat_id=target_chat_id,
                        text=data["text_html"],
                        parse_mode="HTML",
                        reply_markup=data["reply_markup"],
                    )
                break
            except RetryAfter as e:
                logger.warning(f"[NOTIFY] Flood control hit, sleeping {e.retry_after}s for {target_chat_id}")
                await asyncio.sleep(e.retry_after + 0.5)
                continue
            except (Forbidden, BadRequest) as e:
                logger.error(f"[NOTIFY] Skipping {target_chat_id}: {e}")
                break
            except Exception as e:
                logger.error(f"[NOTIFY] Unexpected error for {target_chat_id}: {e}")
                break

        if sent_msg:
            sent_count += 1
        else:
            failed_count += 1

        await asyncio.sleep(NOTIFY_SEND_DELAY)

    await message.reply_text(
        f"✅ Notify sent to {sent_count} account"
        + (f", ❌ {failed_count} failed" if failed_count else "")
        + f"\n🎯 Target: <code>{target_kind}:{target_value}</code>",
        parse_mode="HTML",
    )