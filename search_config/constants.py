import re, requests, logging, httpx
from urllib.parse import urlparse
SEARCH_CHOICE, SEARCH_OPTION, SEARCH_RANGE = range(20, 23)
ANIMEFLIX_EPISODE_RANGE = 90
TOONWORLD_QUALITY = 80
GAMELEECH_PARTS, GAMELEECH_CHOICE = range(2)

TELEGRAM_TEXT_LIMIT = 4096
SAFE_TEXT_LIMIT = 3800   # keep buffer for formatting
MAX_RESULTS = 25         # hard cap to avoid overflow
MAX_RESULTS_PER_SITE = 10
REQUEST_TIMEOUT = 12
EPISODE_REGEX = re.compile(r'\b(ep|episode)\s*\d+\b', re.I)
DRIVE_KEYWORDS = ("drive", "gdrive", "google", "server", "fast", "direct")
BUTTON_KEYWORDS = {
    "Telegram File": ["telegram file", "tg file", "telegram"],
    "Fast Cloud": ["fast cloud"],
    "Instant DL": ["instant dl", "instant download"],
    "Direct Server": ["direct server"]
}
import requests

# Define headers once
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Accept-Encoding": "gzip, deflate, br",
}
# Create one session globally
session = requests.Session()
session.headers.update(HEADERS)

def build_headers(url: str) -> dict:
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
    headers = HEADERS.copy()
    headers["Referer"] = base
    return headers

def escape_markdown_v2(text: str) -> str:
    """
    Properly escape all special characters for Telegram MarkdownV2
    """
    special_chars = r'([_*[\]()~`>#+-=|{}.!])'
    return re.sub(special_chars, r'\\\1', text)

def safe_get(url: str):
    if "links.toonworld4all.me/redirect" in url:
        logging.info("[SAFE_GET] Skipped fetch (direct redirect link)")
        return None  # DO NOT TOUCH THIS URL

    return httpx.get(url, follow_redirects=True, timeout=15)

