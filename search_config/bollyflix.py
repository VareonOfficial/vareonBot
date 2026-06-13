import re
from bs4 import BeautifulSoup
import asyncio
from telegram import Update, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from .constants import SEARCH_OPTION, escape_markdown_v2, safe_get, SEARCH_RANGE
#######################################
# BOLLYFIX DOWNLOAD PROCEDURE
#######################################
def fetch_bollyflix_options(page_url):
    r = safe_get(page_url)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    grouped = []

    for h5 in soup.find_all("h5"):
        span = h5.find("span")
        if not span:
            continue

        title = span.get_text(strip=True)
        p = h5.find_next_sibling("p")
        if not p:
            continue

        # collect all links
        links = []
        for a in p.find_all("a", class_="dl", href=True):
            txt = a.get_text(strip=True).lower()
            href = a["href"]

            if href.startswith(("http://", "https://")):
                links.append({"text": txt, "link": href})

        # apply your conditions
        chosen = None
        # case 1: prefer Google Drive
        for l in links:
            if "drive" in l["text"].lower():
                chosen = l
                break
        # case 2: fallback to Download Links
        if not chosen and links:
            for l in links:
                if "download" in l["text"].lower():
                    chosen = l
                    break
        # case 3: if only one link exists, just use it
        if not chosen and links:
            chosen = links[0]

        if chosen:
            grouped.append({
                "label": title,
                "buttons": [chosen]  # only one button per group
            })

    return grouped

async def choose_bollyflix_result(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    options = fetch_bollyflix_options(url)

    if not options:
        await update.message.reply_text("No download options found.")
        return ConversationHandler.END

    context.user_data['download_options'] = options
    context.user_data['flat_links'] = []

    keyboard = []
    text = "**Available qualities:**\n\n"

    for i, group in enumerate(options, 1):
        safe_label = escape_markdown_v2(group['label'])
        text += f"**[{i}]** {safe_label}\n"

        row = []
        for j, btn in enumerate(group['buttons'], 1):
            display = f"{i}.{j} Download Links"
            callback = f"opt_{i}_{j}"
            row.append(InlineKeyboardButton(display, callback_data=callback))
            context.user_data['flat_links'].append(btn['link'])

        if row:
            keyboard.append(row)

    sent = await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="MarkdownV2"
    )

    context.user_data['quality_msg_id'] = sent.message_id
    return SEARCH_OPTION
    
async def handle_fxlinks_page(query, context, fx_url: str):
    data = fetch_episode_links(fx_url)
    if not data:
        await query.message.reply_text("❌ Failed to parse fxlinks page.")
        return ConversationHandler.END

    episodes = data["episodes"]
    season_zip = data["season_zip"]

    if episodes:
        avail = sorted(episodes.keys())
        context.user_data["episodes"] = episodes

        # Build the text in one string
        text = (
            f"🎞️Episodes found: {avail[0]} – {avail[-1]}\n"
            "Reply with range (e.g. 1-5, 2,4) or single episode number\n\nOR\n\n"
        )

        # If season zip also exists, add a line
        keyboard = None
        if season_zip:
            text += "Season archive also available:\n"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 Season Zip", url=season_zip)]
            ])

        await query.message.reply_text(
            text,
            reply_markup=keyboard
        )
        return SEARCH_RANGE

    # If only season zip exists
    if season_zip:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 Season Zip", url=season_zip)]
        ])
        await query.message.reply_text(
            "Season archive available:",
            reply_markup=keyboard
        )
        return ConversationHandler.END

    await query.message.reply_text("No episodes or season zip found.")
    return ConversationHandler.END

def fetch_episode_links(page_url: str):
    r = safe_get(page_url)
    if not r:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    episodes = {}
    season_zip = None

    for h3 in soup.find_all("h3"):
        a = h3.find("a", href=True)
        if not a:
            continue
        text = a.get_text(strip=True)
        href = a["href"]

        if "season zip" in text.lower():
            season_zip = href
        elif "episode" in text.lower():
            # extract episode number
            m = re.search(r'\d+', text)
            if m:
                num = int(m.group())
                episodes[num] = href

    return {"episodes": episodes, "season_zip": season_zip}

async def extract_bollyflix_link(verify_url):
    command = ["vsearch", "--type", "bollyflix", verify_url]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
)
    stdout, stderr = await process.communicate()
    output = stdout.decode() + stderr.decode()
    url_match = re.search(r"URL:\s+(https?://[^\s]+)", output)
    if url_match:
        return url_match.group(1).strip()
    else:
        print(f"DEBUG: vsearch output was: {output}")
        return None