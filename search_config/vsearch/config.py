import sys, re, logging
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(filename)s:%(lineno)d | %(message)s")
logger = logging.getLogger(__name__)


HEADERS = {
    "accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8"
    ),
    "accept-language": "en-GB,en;q=0.6",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "upgrade-insecure-requests": "1",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    )
}