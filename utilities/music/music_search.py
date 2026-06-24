# music_search.py
import time
import hmac
import hashlib
import logging
import requests
from ytmusicapi import YTMusic
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

SPOTIFY_SECRET_URL  = "https://code.thetadev.de/ThetaDev/spotify-secrets/raw/branch/main/secrets/secretDict.json"
SPOTIFY_CLIENT_ID   = "d8a5ed958d274c2e8ee717e6a4b0971d"
SPOTIFY_APP_VERSION = "1.2.93.439.g60e5e10e"
SPOTIFY_SEARCH_HASH = "63a93cc04f6d8dea84a85de315e43f396a76cb681500de9ac5ccf5fc618c84cb"

_REMIX_KEYWORDS = {
    "slowed", "reverb", "sped up", "spedup", "speed up", "bass boost",
    "bass boosted", "lofi", "lo-fi", "remix", "edit", "dub", "cover",
    "karaoke", "instrumental", "nightcore", "mashup", "best part",
}

_CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"

# ─────────────────────────────────────────────────────────────────────────────
# YouTube Music
# ─────────────────────────────────────────────────────────────────────────────

def _is_original(title: str) -> bool:
    lower = title.lower()
    return not any(kw in lower for kw in _REMIX_KEYWORDS)


def search_youtube_music(query: str) -> dict | None:
    try:
        ytmusic = YTMusic()
        results = ytmusic.search(query, filter="songs", limit=20)

        # prefer original tracks first
        for r in results:
            if _is_original(r.get("title", "")):
                vid     = r.get("videoId")
                artists = ", ".join(a["name"] for a in r.get("artists", []))
                return {
                    "title":    r.get("title", ""),
                    "artist":   artists,
                    "duration": r.get("duration", ""),
                    "yt_url":   f"https://music.youtube.com/watch?v={vid}",
                }

        # fallback to top result if all are remixes
        if results:
            r       = results[0]
            vid     = r.get("videoId")
            artists = ", ".join(a["name"] for a in r.get("artists", []))
            return {
                "title":    r.get("title", ""),
                "artist":   artists,
                "duration": r.get("duration", ""),
                "yt_url":   f"https://music.youtube.com/watch?v={vid}",
            }
    except Exception as e:
        logger.error("[MUSIC SEARCH] YTMusic error: %s", e)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Spotify auth
# ─────────────────────────────────────────────────────────────────────────────

def _get_spotify_access_token(sp_dc: str) -> str | None:
    try:
        r = requests.get(SPOTIFY_SECRET_URL, timeout=10)
        r.raise_for_status()
        data = r.json()
        ver  = list(data.keys())[-1]
        arr  = data[ver]
        key  = "".join(str(v ^ ((i % 33) + 9)) for i, v in enumerate(arr)).encode()

        counter = int(time.time() // 30)
        h       = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
        offset  = h[-1] & 0x0F
        binary  = (
            (h[offset]   & 0x7F) << 24 | (h[offset+1] & 0xFF) << 16 |
            (h[offset+2] & 0xFF) << 8  | (h[offset+3] & 0xFF)
        )
        code = str(binary % 1000000).zfill(6)

        s = requests.Session()
        s.headers.update({"User-Agent": _CHROME_UA})
        s.cookies.set("sp_dc", sp_dc, domain="open.spotify.com")

        r = s.get("https://open.spotify.com/api/token", params={
            "reason": "init", "productType": "web-player",
            "totp": code, "totpVer": ver, "totpServer": code,
        }, timeout=10)
        r.raise_for_status()
        return r.json().get("accessToken")
    except Exception as e:
        logger.error("[MUSIC SEARCH] Spotify access token error: %s", e)
    return None


def _get_spotify_client_token() -> str | None:
    try:
        r = requests.post(
            "https://clienttoken.spotify.com/v1/clienttoken",
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": "https://open.spotify.com",
                "referer": "https://open.spotify.com/",
                "User-Agent": _CHROME_UA,
            },
            json={"client_data": {
                "client_version": SPOTIFY_APP_VERSION,
                "client_id":      SPOTIFY_CLIENT_ID,
                "js_sdk_data": {
                    "device_brand": "unknown", "device_model": "unknown",
                    "os": "windows",           "os_version":   "NT 10.0",
                    "device_id":    "52f72a2703ad7602f0c443105ad1bd09",
                    "device_type":  "computer",
                }
            }},
            timeout=10
        )
        r.raise_for_status()
        return r.json()["granted_token"]["token"]
    except Exception as e:
        logger.error("[MUSIC SEARCH] Spotify client-token error: %s", e)
    return None


def search_spotify(query: str, sp_dc: str) -> str | None:
    access_token = _get_spotify_access_token(sp_dc)
    client_token = _get_spotify_client_token()
    if not access_token or not client_token:
        return None
    try:
        r = requests.post(
            "https://api-partner.spotify.com/pathfinder/v2/query",
            headers={
                "Authorization":       f"Bearer {access_token}",
                "client-token":        client_token,
                "App-Platform":        "WebPlayer",
                "content-type":        "application/json;charset=UTF-8",
                "accept":              "application/json",
                "accept-language":     "en-GB",
                "origin":              "https://open.spotify.com",
                "referer":             "https://open.spotify.com/",
                "sec-fetch-dest":      "empty",
                "sec-fetch-mode":      "cors",
                "sec-fetch-site":      "same-site",
                "sec-ch-ua":           '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
                "sec-ch-ua-mobile":    "?0",
                "sec-ch-ua-platform":  '"Windows"',
                "spotify-app-version": SPOTIFY_APP_VERSION,
                "User-Agent":          _CHROME_UA,
            },
            json={
                "variables": {
                    "query":                          query,
                    "limit":                          5,
                    "offset":                         0,
                    "numberOfTopResults":             1,
                    "includeArtistHasConcertsField":  False,
                    "includeAudiobooks":              True,
                    "includeAuthors":                 False,
                    "includePreReleases":             True,
                    "includeAlbumPreReleases":        True,
                    "includeEpisodeContentRatingsV2": True,
                    "isPrefix":                       None,
                    "sectionFilters":                 ["GENERIC", "VIDEO_CONTENT"],
                },
                "operationName": "searchTopResultsList",
                "extensions": {"persistedQuery": {
                    "version":    1,
                    "sha256Hash": SPOTIFY_SEARCH_HASH,
                }}
            },
            timeout=10
        )
        if r.status_code != 200:
            logger.warning("[MUSIC SEARCH] Spotify search HTTP %s", r.status_code)
            return None

        items = r.json()["data"]["searchV2"]["topResultsV2"]["itemsV2"]
        for item in items:
            d = item.get("item", {}).get("data", {})
            if d.get("__typename") == "Track":
                track_id = d["uri"].split(":")[-1]
                return f"https://open.spotify.com/track/{track_id}"
    except Exception as e:
        logger.error("[MUSIC SEARCH] Spotify search error: %s", e)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point — called from music() handler
# ─────────────────────────────────────────────────────────────────────────────

async def search_and_show(query: str, update, context, vareon_id: str, sp_dc: str | None) -> None:
    """
    Search YouTube Music + Spotify, show inline buttons.
    On button click → music_search_pick_callback() fires and resumes normal flow.
    """
    searching_msg = await update.message.reply_text("🔍 Searching...")

    yt_result = search_youtube_music(query)
    if not yt_result:
        await searching_msg.edit_text("❌ No results found.")
        return

    title    = yt_result["title"]
    artist   = yt_result["artist"]
    duration = yt_result["duration"]
    yt_url   = yt_result["yt_url"]

    # search spotify using title + artist for better accuracy
    sp_url = None
    if sp_dc:
        sp_url = search_spotify(f"{title} {artist}", sp_dc)

    # store URLs in context.user_data — callback_data has a 64 byte Telegram limit
    context.user_data["music_search_links"] = {
        "yt": yt_url,
        "sp": sp_url,
    }

    buttons = [[InlineKeyboardButton(f"▶️  YouTube — {title}", callback_data="music_search_pick:yt")]]
    if sp_url:
        buttons.append([InlineKeyboardButton(f"🟢  Spotify — {title}", callback_data="music_search_pick:sp")])

    await searching_msg.edit_text(
        f"🎵 *Search results found*\n\n"
        f"*{title}* — {artist}\n"
        f"⏱ {duration}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )