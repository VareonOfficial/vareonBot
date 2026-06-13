import re
from bs4 import BeautifulSoup
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from .constants import SEARCH_OPTION, EPISODE_REGEX , SEARCH_RANGE,  escape_markdown_v2, safe_get
from .utils import parse_driveseed_buttons, build_driveseed_keyboard
#######################################
# MODLIST DOWNLOAD PROCEDURE
#######################################

def resolve_modlist_domain(type_name):
    url = f"https://modlist.in/?type={type_name}"
    r = safe_get(url)
    if not r:
        return None

    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    meta = soup.find("meta", attrs={"http-equiv": "refresh"})
    if meta:
        content = meta.get("content", "")
        if "url=" in content:
            return content.split("url=")[-1].strip().rstrip("/")

    match = re.search(r'window\.location\.href\s*=\s*["\'](.*?)["\']', html)
    if match:
        return match.group(1).rstrip("/")

    return None



def fetch_download_options(page_url):
    r = safe_get(page_url)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    grouped = []
    seen = set()

    def clean(text):
        return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()

    def is_button_block(tag):
        return (
            tag.name == "p"
            and tag.find("a", class_=re.compile("maxbutton"))
        )

    def extract_buttons(p):
        buttons = []
        for a in p.find_all("a", href=True, class_=re.compile("maxbutton")):
            txt = a.get_text(strip=True)
            link = a["href"]
            if link.startswith(("http://", "https://")):
                buttons.append({"text": txt or "Download", "link": link})
        return buttons

    blocks = list(soup.find_all(["h3", "h4", "p"]))

    for i, block in enumerate(blocks):
        # ---------- identify TITLE ----------
        label = None

        if block.name in ("h3", "h4"):
            label = clean(block.get_text())

        elif block.name == "p":
            if block.find("a", class_=re.compile("maxbutton")):
                continue  # this is a button block, not title

            strong = block.find("strong")
            if strong:
                label = clean(strong.get_text())
            else:
                txt = clean(block.get_text())
                if 20 <= len(txt) <= 200:
                    label = txt

        if not label or label in seen:
            continue

        # ---------- next block must be buttons ----------
        if i + 1 >= len(blocks):
            continue

        next_block = blocks[i + 1]
        if not is_button_block(next_block):
            continue

        buttons = extract_buttons(next_block)
        if not buttons:
            continue

        grouped.append({
            "label": label,
            "buttons": buttons
        })
        seen.add(label)

    return grouped

def fetch_host_links(page_url):
    """Get server/host links from episode or batch page"""
    r = safe_get(page_url)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    hosts = []

    for span in soup.find_all("span", class_=re.compile("mb-text")):
        label = span.get_text(strip=True)
        parent_a = span.find_parent("a", href=True)
        if label and parent_a and parent_a["href"]:
            hosts.append({"label": label, "link": parent_a["href"]})

    return hosts
        
def parse_episode_range(user_input, available_numbers):
    try:
        start, end = map(int, user_input.split("-"))
        if end - start + 1 > 10:
            raise ValueError("Max 10 episodes allowed")
        selected = [ep for ep in range(start, end + 1) if ep in available_numbers]
        return selected
    except Exception as e:
        raise e


def fetch_episode_links(page_url):
    r = safe_get(page_url)
    if not r:
        return {}

    soup = BeautifulSoup(r.text, "html.parser")
    episodes = {}

    for h3 in soup.find_all("h3"):
        a = h3.find("a", href=True)
        if not a:
            continue
        text = a.get_text(strip=True)
        if not text:
            continue
        m = EPISODE_REGEX.search(text)
        if m:
            num = int(re.search(r"\d+", text).group())
            episodes[num] = a["href"]

    return episodes


async def choose_moviesmod_result(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    options = fetch_download_options(url)

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
            btn_label = escape_markdown_v2(btn.get('text', 'Download'))  # use actual label
            display = f"{i}.{j} {btn_label}"
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

async def process_single_link(query, context, chosen):
    status_msg = await query.message.reply_text("⏳ Resolving final download link…")

    final = await extract_moviesmod_link(chosen)

    if not final:
        await status_msg.edit_text("❌ Failed to resolve final link.")
        return

    actions = parse_driveseed_buttons(final)
    keyboard = build_driveseed_keyboard(actions)

    text = f"✅ Final link resolved:\n{final}"

    await status_msg.edit_text(
        text,
        reply_markup=keyboard
    )
  
async def handle_episode_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chosen = []
    try:
        if '-' in text:
            start, end = map(int, text.split('-'))
            chosen = list(range(start, end + 1))
        elif ',' in text:
            chosen = [int(part.strip()) for part in text.split(',')]
        else:
            chosen = [int(text.strip())]
    except Exception:
        await update.message.reply_text("Invalid range. Example: 1-5 or 3 or 2,4")
        return SEARCH_RANGE

    episodes = context.user_data.get("episodes", {})
    valid_eps = [e for e in chosen if e in episodes]

    if not valid_eps:
        await update.message.reply_text("No valid episodes selected.")
        return ConversationHandler.END

    progress_text = "⏳ Processing episodes...\n\n"
    progress_msg = await update.message.reply_text(progress_text)

    for ep in valid_eps:
        progress_text += f"▶ Processing Episode {ep}\n"
        await progress_msg.edit_text(progress_text)

        link = episodes[ep]

        # Special case: fastdlserver.life links
        if "fastdlserver" in link:
            r = safe_get(link)
            final = r.url if r and r.url else None
        else:
            final = await extract_moviesmod_link(link)

        if final:
            progress_text += f"Episode {ep} → {final}\n"

            actions = parse_driveseed_buttons(final)
            if "telegram" in actions:
                progress_text += f"Telegram File - {actions['telegram']}\n\n"
            else:
                progress_text += "\n"
        else:
            progress_text += f"Episode {ep} → ❌ Failed\n\n"

        await progress_msg.edit_text(progress_text)

    return ConversationHandler.END

async def extract_moviesmod_link(verify_url):
    command = ["vsearch", "--type", "moviesmod", verify_url]
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