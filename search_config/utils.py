from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from playwright.async_api import async_playwright
from .constants import BUTTON_KEYWORDS, safe_get
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs, unquote, urljoin
from main.config import CDP_URL
import aiohttp

def format_telegram_deeplink(url):
    """
    Converts telegram link URL to tg://resolve deep link
    """
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)

        start = qs.get("start", [None])[0]
        bot = qs.get("bot", [None])[0]

        if not start or not bot:
            return None

        start = unquote(start)  # decode %3D%3D → ==

        return f"tg://resolve?domain={bot}&start={start}"

    except Exception:
        return None
    
#################################
# GDFlix specific functions
#################################
async def build_final_link_buttons(final_url):
    buttons = []
    links = []

    async with aiohttp.ClientSession() as session:
        async with session.get(final_url, timeout=60) as resp:
            html = await resp.text()

    soup = BeautifulSoup(html, "html.parser")

    # Extract all anchor tags
    for a in soup.select("a[href]"):
        text = (a.get_text(strip=True) or "").lower()
        href = a.get("href")

        if not href:
            continue
        href = urljoin(final_url, href)

        links.append({
            "text": text,
            "href": href
        })
    for label, keywords in BUTTON_KEYWORDS.items():
        for link in links:
            if any(k in link["text"] for k in keywords):
                url = link["href"]

                tg_deep = format_telegram_deeplink(url)
                if tg_deep:
                    url = tg_deep

                buttons.append([
                    InlineKeyboardButton(label, url=url)
                ])
                break

    if buttons:
        return InlineKeyboardMarkup(buttons)
    return None

#################################
# DriveSeed specific functions
#################################
def parse_driveseed_buttons(final_url):
    """
    Parses DriveSeed file page and extracts available action buttons.
    Returns dict with possible keys:
    telegram, instant, instant_v2, resume, cloud
    """
    r = safe_get(final_url)
    if not r:
        return {}

    soup = BeautifulSoup(r.text, "html.parser")
    actions = {}

    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        href = a["href"]

        if not href.startswith(("http://", "https://")):
            continue

        if "telegram" in text:
            deep_link = format_telegram_deeplink(href)
            if deep_link:
                actions["telegram"] = deep_link

        elif "instant download v2" in text:
            actions["instant_v2"] = href

        elif "instant download" in text:
            actions["instant"] = href

        elif "resume cloud" in text:
            actions["resume"] = href

        elif "cloud download" in text or "direct download" in text:
            actions["cloud"] = href

    return actions

def build_driveseed_keyboard(actions: dict) -> InlineKeyboardMarkup | None:
    """
    Converts DriveSeed actions dict into Telegram InlineKeyboardMarkup.
    """
    keyboard = []

    if actions.get("telegram"):
        keyboard.append([
            InlineKeyboardButton("Open in Telegram ↗", url=actions["telegram"])
        ])

    if actions.get("instant"):
        keyboard.append([
            InlineKeyboardButton("Instant Download", url=actions["instant"])
        ])

    if actions.get("instant_v2"):
        keyboard.append([
            InlineKeyboardButton("Instant Download V2", url=actions["instant_v2"])
        ])

    if actions.get("resume"):
        keyboard.append([
            InlineKeyboardButton("Resume Download", url=actions["resume"])
        ])
    elif actions.get("cloud"):
        keyboard.append([
            InlineKeyboardButton("Cloud Download", url=actions["cloud"])
        ])

    return InlineKeyboardMarkup(keyboard) if keyboard else None
