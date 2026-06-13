import re, logging, httpx
from typing import final
import requests
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import asyncio
import time
from playwright.async_api import async_playwright
from telegram import Update, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from .moviesmod import parse_driveseed_buttons
from .constants import SEARCH_OPTION, ANIMEFLIX_EPISODE_RANGE,  escape_markdown_v2, safe_get
#######################################
# ANIMEFLIX DOWNLOAD PROCEDURE
#######################################

def fetch_animeflix_options(page_url):
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

    def extract_gdrive_button(p):
        for a in p.find_all("a", href=True, class_=re.compile("maxbutton")):
            text = a.get_text(strip=True).lower()
            href = a["href"]

            if not href.startswith(("http://", "https://")):
                continue

            # ✅ Only GDrive / Google / Drive mirrors
            if any(k in text for k in ("gdrive", "google", "drive")):
                return {"text": text, "link": href}

        return None

    blocks = list(soup.find_all(["h3", "p"]))

    for i, block in enumerate(blocks):
        label = None

        # Case 1: <h3> title
        if block.name == "h3":
            label = clean(block.get_text())

        # Case 2: <p><strong>Quality</strong></p>
        elif block.name == "p":
            strong = block.find("strong")
            if strong:
                label = clean(strong.get_text())

        if not label or label in seen:
            continue

        if i + 1 >= len(blocks):
            continue

        next_block = blocks[i + 1]
        if not is_button_block(next_block):
            continue

        chosen = extract_gdrive_button(next_block)
        if not chosen:
            continue

        grouped.append({
            "label": label,
            "buttons": [chosen]
        })
        seen.add(label)

    return grouped

async def choose_animeflix_result(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    options = fetch_animeflix_options(url)

    if not options:
        await update.message.reply_text("No Google Drive download options found.")
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

async def resolve_animeflix_listing(message, context, url: str):
    r = safe_get(url)
    if not r:
        await message.reply_text("❌ Failed to load Animeflix page.")
        return ConversationHandler.END

    soup = BeautifulSoup(r.text, "html.parser")

    # =======================
    # Detect episodes
    # =======================
    episode_links = []
    for h3 in soup.find_all("h3"):
        a = h3.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True).lower()
        if "episode" in title:
            num = ''.join(filter(str.isdigit, title))
            if num:
                episode_links.append((int(num), a["href"]))

    if episode_links:
        episode_links.sort()

        # ✅ ONLY ONE EPISODE → AUTO RESOLVE
        if len(episode_links) == 1:
            ep_no, ep_url = episode_links[0]

            await message.reply_text(f"▶️ Processing Episode {ep_no}...")

            final_link = await resolve_final_link(ep_url)
            actions = parse_driveseed_buttons(final_link)

            text = (
                f"▶️ Episode {ep_no}\n"
                f"{final_link}\n"
            )

            rows = []
            if "telegram" in actions:
                rows.append([InlineKeyboardButton("Telegram File", url=actions["telegram"])])
            if "instant" in actions:
                rows.append([InlineKeyboardButton("Instant Download", url=actions["instant"])])
            if "instant_v2" in actions:
                rows.append([
                    InlineKeyboardButton("Instant Download V2", url=actions["instant_v2"])
                ])
            if "resume" in actions:
                rows.append([
                    InlineKeyboardButton("Resume Download", url=actions["resume"])
                ])
            if "cloud" in actions:
                rows.append([
                    InlineKeyboardButton("Cloud Download", url=actions["cloud"])
                ])

            markup = InlineKeyboardMarkup(rows) if rows else None

            await message.reply_text(text, reply_markup=markup)
            return ConversationHandler.END

        # 🔁 MULTIPLE EPISODES → ASK RANGE
        context.user_data["animeflix_episodes"] = dict(episode_links)
        first = episode_links[0][0]
        last = episode_links[-1][0]

        await message.reply_text(
            f"🎞️ Episodes found: {first} – {last}\n\n"
            "Reply with range (e.g. 1-5, 2,4) or single episode number."
        )
        return ANIMEFLIX_EPISODE_RANGE

    # =======================
    # Detect movies
    # =======================
    movies = []
    for h3 in soup.find_all("h3"):
        a = h3.find("a", href=True)
        if not a:
            continue
        txt = a.get_text(strip=True)
        if "movie" in txt.lower():
            movies.append({"label": txt, "link": a["href"]})

    if movies:
        # ✅ ONLY ONE MOVIE → AUTO RESOLVE
        if len(movies) == 1:
            movie = movies[0]

            await message.reply_text(f"🎬 Processing movie...\n{movie['label']}")

            final_link = await resolve_final_link(movie["link"])
            actions = parse_driveseed_buttons(final_link)

            text = f"{movie['label']}\n{final_link}\n"

            rows = []
            if "telegram" in actions:
                rows.append([InlineKeyboardButton("Telegram File", url=actions["telegram"])])
            if "instant" in actions:
                rows.append([InlineKeyboardButton("Instant Download", url=actions["instant"])])
            if "instant_v2" in actions:
                rows.append([
                    InlineKeyboardButton("Instant Download V2", url=actions["instant_v2"])
                ])
            if "resume" in actions:
                rows.append([
                    InlineKeyboardButton("Resume Download", url=actions["resume"])
                ])
            if "cloud" in actions:
                rows.append([
                    InlineKeyboardButton("Cloud Download", url=actions["cloud"])
                ])

            markup = InlineKeyboardMarkup(rows) if rows else None

            await message.reply_text(text, reply_markup=markup)
            return ConversationHandler.END

        # 🔁 MULTIPLE MOVIES → SHOW SELECTION
        keyboard = [
            [InlineKeyboardButton(m["label"], callback_data=f"anime_movie_{i}")]
            for i, m in enumerate(movies)
        ]
        context.user_data["animeflix_movies"] = movies

        await message.reply_text(
            "🎬 Movies found:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return SEARCH_OPTION

    await message.reply_text("❌ No episodes or movies found.")
    return ConversationHandler.END


async def animeflix_episode_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    eps = context.user_data.get("animeflix_episodes", {})

    if not eps:
        await update.message.reply_text("❌ Episode data expired.")
        return ConversationHandler.END

    # Parse input
    chosen, missing = [], []

    if "-" in text:
        start, end = map(int, text.split("-"))
        for i in range(start, end + 1):
            (chosen if i in eps else missing).append(i)

    elif "," in text:
        for part in text.split(","):
            try:
                i = int(part.strip())
                (chosen if i in eps else missing).append(i)
            except ValueError:
                continue

    else:
        try:
            i = int(text)
            (chosen if i in eps else missing).append(i)
        except ValueError:
            await update.message.reply_text("❌ Invalid input format.")
            return ConversationHandler.END

    # Initial message
    progress_text = "⏳ Processing episodes...\n\n"
    progress_msg = await update.message.reply_text(progress_text)

    last_text = progress_text
    last_markup = None

    # 🔑 show buttons ONLY for single episode
    show_buttons = (len(chosen) == 1)

    # Process episodes
    for ep in chosen:
        final_link = await resolve_final_link(eps[ep])
        actions = parse_driveseed_buttons(final_link)

        progress_text += f"▶️ Processing Episode {ep}\n"
        progress_text += f"Episode {ep} → {final_link}\n"

        if "telegram" in actions:
            progress_text += f"Telegram File - {actions['telegram']}\n"

        progress_text += "\n"

        # ❌ NO buttons for range
        markup = None

        # ✅ Buttons ONLY for single episode
        if show_buttons:
            rows = []
            if "telegram" in actions:
                rows.append([
                    InlineKeyboardButton("Telegram File", url=actions["telegram"])
                ])
            if "instant" in actions:
                rows.append([
                    InlineKeyboardButton("Instant Download", url=actions["instant"])
                ])
            if "instant_v2" in actions:
                rows.append([
                    InlineKeyboardButton("Instant Download V2", url=actions["instant_v2"])
                ])
            if "resume" in actions:
                rows.append([
                    InlineKeyboardButton("Resume Download", url=actions["resume"])
                ])
            if "cloud" in actions:
                rows.append([
                    InlineKeyboardButton("Cloud Download", url=actions["cloud"])
                ])

            markup = InlineKeyboardMarkup(rows) if rows else None

        # 🔐 Edit only if changed
        if progress_text != last_text or markup != last_markup:
            try:
                await progress_msg.edit_text(progress_text, reply_markup=markup)
                last_text = progress_text
                last_markup = markup
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

    # Report missing episodes (append once)
    if missing:
        for ep in missing:
            progress_text += f"❗ Episode {ep} was not found\n"

        if progress_text != last_text:
            try:
                await progress_msg.edit_text(progress_text)
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise

    return ConversationHandler.END

async def resolve_final_link(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://driveseed.org/"
            }
        ) as client:

            r = await client.get(url)
            final_url = str(r.url)

            # If already a file link, return
            if "/file/" in final_url:
                return final_url

            # Handle /r?... pages
            if "driveseed.org/r" in final_url:
                soup = BeautifulSoup(r.text, "html.parser")

                # 1️⃣ <a href="/file/...">
                a = soup.find("a", href=True)
                if a and "/file/" in a["href"]:
                    return "https://driveseed.org" + a["href"]

                # 2️⃣ <meta http-equiv="refresh">
                meta = soup.find("meta", attrs={"http-equiv": "refresh"})
                if meta:
                    content = meta.get("content", "")
                    if "url=" in content.lower():
                        return content.split("url=")[-1].strip()

                # 3️⃣ JavaScript redirect (MOST IMPORTANT)
                scripts = soup.find_all("script")
                for s in scripts:
                    if not s.string:
                        continue

                    m = re.search(
                        r'(?:window\.location(?:\.href)?|location\.replace)\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)',
                        s.string
                    )
                    if m:
                        link = m.group(1)
                        if link.startswith("/"):
                            return "https://driveseed.org" + link
                        return link

            return final_url

    except Exception as e:
        logging.error(f"Failed to resolve {url}: {e}")
        return None
