import re
import logging
from typing import final
import requests
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import asyncio
import time
from playwright.async_api import async_playwright
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from .constants import SEARCH_OPTION, escape_markdown_v2, safe_get
from main.config import logger

# -----------------------------------------------------------------------------
# KATWAP DOWNLOAD PROCEDURE
# -----------------------------------------------------------------------------

def fetch_katwap_options(page_url: str) -> list:
    """
    Parses the KatWap download page and extracts quality + direct download links.
    Returns list of dicts like:
    [
        {"label": "480p x264 AAC HC [770MB]", "link": "https://nexdrive.best/..."},
        {"label": "720p x265 HEVC HC [1.19GB]", "link": "..."},
        ...
    ]
    """
    r = safe_get(page_url)
    if not r:
        logger.warning(f"Failed to fetch page: {page_url}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    download_div = soup.find("div", class_="download-links-div")
    if not download_div:
        logger.info("No .download-links-div found on page")
        return []

    options = []
    current_quality = None

    for elem in download_div.find_all(["h3", "a"]):
        if elem.name == "h3":
            span = elem.find("span", style=re.compile("color.*#ff0000", re.I))
            if span:
                current_quality = span.get_text(strip=True)

        elif elem.name == "a" and elem.get("href"):
            href = elem["href"]
            if not href.startswith(("http://", "https://")):
                continue

            link_text = elem.get_text(strip=True)

            size_match = re.search(r'\[([^\]]+)\]', link_text)
            size = size_match.group(1).strip() if size_match else ""

            if current_quality:
                label = f"{current_quality}"
                if size:
                    label += f"  [{size}]"
            else:
                label = link_text.strip("Click Here To Download ").strip()

            options.append({
                "label": label,
                "link": href
            })

    seen = set()
    unique_options = []
    for opt in options:
        if opt["link"] not in seen:
            seen.add(opt["link"])
            unique_options.append(opt)

    return unique_options


async def choose_katwap_result(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    """
    Fetches download options from KatWap page and shows inline buttons
    Very similar pattern to choose_bollyflix_result
    """
    options = fetch_katwap_options(url)

    if not options:
        await update.message.reply_text("No download options found on this page.")
        return ConversationHandler.END

    context.user_data['download_options'] = [
        {"buttons": options}
    ]
    context.user_data['flat_links'] = [opt["link"] for opt in options]

    keyboard = []
    text = "**Available Download Options:**\n\n"

    for i, opt in enumerate(options, 1):
        safe_label = escape_markdown_v2(opt["label"])
        text += f"**{i}\\.** {safe_label}\n"

        btn_text = f"{i} • Download Now"
        callback_data = f"opt_1_{i}"
        keyboard.append([
            InlineKeyboardButton(btn_text, callback_data=callback_data)
        ])

    sent = await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="MarkdownV2",
        disable_web_page_preview=True
    )

    context.user_data['quality_msg_id'] = sent.message_id
    return SEARCH_OPTION

async def extract_katwap_link(query, context, page_url: str):
    """
    Parses the KatWap final page (nexdrive.best / .pics / etc.)
    Shows links directly — no fast-dl resolution anymore.
    """
    r = safe_get(page_url)
    if not r or r.status_code != 200:
        await query.message.reply_text("❌ Could not load the page.")
        return ConversationHandler.END

    soup = BeautifulSoup(r.text, "html.parser")

    priority = [
        ("fast-dl.org",     "G-Direct [Instant]"),
        ("vcloud.ac",       "V-Cloud [Resumable]"),
        ("filepress.app",   "Filepress [G-Drive]"),
        ("gdtot.io",        "GDToT [G-Drive]"),
    ]

    # ────────────────────────────────────────────────
    # CASE: Episodes layout
    # ────────────────────────────────────────────────
    episode_blocks = soup.find_all("div", class_="ep-buttons-wrap")
    if episode_blocks:
        lines = []
        for block in episode_blocks:
            ep_title = block.find_previous_sibling("h4", class_="ep-title-h4")
            label = "Episode ?"
            if ep_title and ep_title.strong:
                label = ep_title.strong.get_text(strip=True).replace("-: Episodes:", "Episode").strip()

            a_gdirect = block.find("a", href=re.compile(r"fast-dl\.org", re.I))
            if a_gdirect and a_gdirect["href"]:
                link = a_gdirect["href"]
                display_text = a_gdirect.get_text(strip=True) or "G-Direct"
                safe_label = escape_markdown_v2(label)
                safe_text = escape_markdown_v2(display_text)

                lines.append(f"{safe_label} : [{safe_text}]({link})")

            elif block.find("a", href=True):
                a = block.find("a", href=True)
                link = a["href"]
                txt = a.get_text(strip=True) or "Download"
                safe_label = escape_markdown_v2(label)
                safe_txt = escape_markdown_v2(txt)

                lines.append(f"{safe_label} : [{safe_txt}]({link})")

        if lines:
            text = "🎬 Episodes found\n\n" + "\n".join(lines)
            await query.message.reply_text(
                text,
                parse_mode="MarkdownV2",
                disable_web_page_preview=True
            )
        else:
            await query.message.reply_text("No usable episode links found.")
        
        return ConversationHandler.END

    # ────────────────────────────────────────────────
    # CASE: Single movie — top buttons
    # ────────────────────────────────────────────────
    top_p = soup.find("p", style=re.compile(r"text-align:\s*center", re.I))
    if top_p:
        anchors = top_p.find_all("a", href=True)
        if not anchors:
            await query.message.reply_text("No download buttons found.")
            return ConversationHandler.END

        found = {}
        for a in anchors:
            href = a["href"].strip()
            for domain, label in priority:
                if domain in href.lower():
                    found[domain] = {
                        "url": href,
                        "label": label
                    }
                    break

        if not found:
            await query.message.reply_text("None of the priority hosts were found.")
            return ConversationHandler.END

        text = "🎥 Download Options\n\n"
        keyboard = []

        for domain, lbl in priority:
            if domain in found:
                info = found[domain]
                text += f"• {lbl} → {info['url']}\n"
                keyboard.append([InlineKeyboardButton(lbl, url=info['url'])])

        await query.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True
        )
        return ConversationHandler.END

    await query.message.reply_text(
        f"Page loaded but format not recognized.\nURL: {page_url}",
        disable_web_page_preview=True
    )
    return ConversationHandler.END