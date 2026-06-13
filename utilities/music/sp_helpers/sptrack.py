"""
sptrack.py
──────────
Spotify metadata extraction via spotify_scraper.SpotifyClient
YouTube matching via Playwright (find_best_youtube_match unchanged)

Key design points
  • vareon_id  = integer Telegram user ID  (NOT the message object)
  • Spotify cookies read from Netscape file: user_data/cookies/spotify/{user_id}.txt
  • YouTube cookies read from:              user_data/cookies/youtube/{user_id}.txt
  • Playlists / albums: collect all track URLs first, then fetch each individually
  • Human-like random delays between Spotify API calls (bot-detection avoidance)
  • Single track: sends a rich preview card to Telegram before YouTube search
  • update_progress / cancellation contract identical to the old browser version
"""

from __future__ import annotations

import asyncio
import random
import re
from ytmusicapi import YTMusic
from rapidfuzz import fuzz
ytmusic = YTMusic()
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Optional

from main.config import logger, COOKIES_PATH
from utilities.music.utils import update_progress
from spotify_scraper import SpotifyClient

BASE_DIR = Path.cwd()


# ═══════════════════════════════════════════════════════════════════════════════
# Cookie helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _load_spotify_cookies(vareon_id: str) -> dict[str, str]:
    """
    Parse the Netscape cookie file for *user_id*.
    Returns dict with sp_dc / sp_key (empty strings when absent).
    """
    cookie_path = COOKIES_PATH / "spotify" / f"{vareon_id}.txt"
    cookies: dict[str, str] = {"sp_dc": "", "sp_key": ""}

    if not cookie_path.exists():
        logger.warning("Spotify cookie file not found: %s", cookie_path)
        return cookies

    jar = MozillaCookieJar(str(cookie_path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        logger.error("Failed to load Spotify cookie file %s: %s", cookie_path, exc)
        return cookies

    for cookie in jar:
        if cookie.name in cookies:
            cookies[cookie.name] = cookie.value or ""

    logger.debug(
        "Loaded Spotify cookies for user %s  sp_dc=%s  sp_key=%s",
        vareon_id, bool(cookies["sp_dc"]), bool(cookies["sp_key"]),
    )
    return cookies


def _make_client(vareon_id: int) -> SpotifyClient:
    """Return an authenticated SpotifyClient, falling back to anonymous."""
    creds = _load_spotify_cookies(vareon_id)
    if creds["sp_dc"] or creds["sp_key"]:
        logger.info("Using authenticated SpotifyClient for user %s", vareon_id)
        return SpotifyClient(cookies=creds)
    logger.info("No Spotify credentials for user %s — anonymous client", vareon_id)
    return SpotifyClient()


# ═══════════════════════════════════════════════════════════════════════════════
# Human-like delay helpers  (avoid Spotify bot detection)
# ═══════════════════════════════════════════════════════════════════════════════

async def _human_delay(min_s: float = 0.8, max_s: float = 2.2) -> None:
    """Random sleep that mimics a human reading between requests."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _burst_delay(idx: int, burst_size: int = 8) -> None:
    """
    After every *burst_size* requests take a longer pause (3-6 s)
    to avoid sustained-rate detection.
    """
    if idx > 0 and idx % burst_size == 0:
        pause = random.uniform(3.0, 6.0)
        logger.debug("Burst pause after %d tracks: %.1fs", idx, pause)
        await asyncio.sleep(pause)

# ═══════════════════════════════════════════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════════════════════════════════════════

def _invalid(track: dict) -> bool:
    if not track:
        return True
    title   = (track.get("title")   or "").lower()
    artists = (track.get("artists") or "").lower()
    if "something went wrong" in title:
        logger.debug("Invalid title detected: %s", title)
        return True
    if not title or title == "unknown title":
        logger.debug("Empty / placeholder title")
        return True
    if "unknown artist" in artists or not artists:
        logger.debug("Invalid artist: %s", artists)
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# Data helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _best_cover(images: list[dict]) -> Optional[str]:
    """Return the highest-res Spotify cover URL (640 × 640)."""
    if not images:
        return None
    url: str = images[0].get("url", "")
    MARKER = "ab67616d0000"
    if MARKER in url:
        idx = url.find(MARKER) + len(MARKER)
        url = url[:idx] + "b273" + url[idx + 4:]   # b273 = 640 px size token
    return url or None


def _resolve_track_url(raw: dict) -> str:
    """Best Spotify track URL from a raw dict (4 fallback strategies)."""
    ext = (raw.get("external_urls") or {}).get("spotify")
    if ext:
        return ext
    if raw.get("url"):
        return raw["url"]
    if raw.get("id"):
        return f"https://open.spotify.com/track/{raw['id']}"
    uri = raw.get("uri") or ""
    if uri:
        return f"https://open.spotify.com/track/{uri.split(':')[-1]}"
    return ""


def _parse_track(raw: dict, override_url: Optional[str] = None) -> dict:
    """Convert a SpotifyClient track dict to the canonical bot shape."""
    artists_list = raw.get("artists") or []
    artists = ", ".join(a["name"] for a in artists_list if a.get("name"))

    # writers / composers — spotify_scraper may expose these
    writers_list = raw.get("writers") or raw.get("composers") or []
    writers = ", ".join(
        w["name"] for w in writers_list if w.get("name")
    ) if writers_list else None

    album_data   = raw.get("album") or {}
    album_title  = album_data.get("name")
    images       = album_data.get("images") or []

    duration_ms: int = raw.get("duration_ms") or 0
    duration_s  = duration_ms // 1000
    duration_str = f"{duration_s // 60}:{duration_s % 60:02d}" if duration_s else None

    release_date = album_data.get("release_date") or raw.get("release_date")

    url = override_url or _resolve_track_url(raw)

    return {
        "url":          url,
        "title":        (raw.get("name") or "Unknown Title").strip(),
        "artists":      artists or "Unknown Artist",
        "writers":      writers,
        "album":        album_title.strip() if album_title else None,
        "release_date": release_date,
        "duration":     duration_str,
        "duration_ms":  duration_ms,
        "coverUrl":     _best_cover(images),
    }

# ═══════════════════════════════════════════════════════════════════════════════
# URL collectors for collections
# ═══════════════════════════════════════════════════════════════════════════════

def _collect_urls_from_playlist(data: dict) -> list[str]:
    urls: list[str] = []
    for item in data.get("tracks") or []:
        track = item.get("track") if "track" in item else item
        if not track:
            continue
        url = _resolve_track_url(track)
        if url:
            urls.append(url)
    logger.info("Collected %d track URLs from playlist", len(urls))
    return urls


def _collect_urls_from_album(data: dict) -> list[str]:
    urls: list[str] = []
    for item in data.get("tracks") or []:
        track = item.get("track") if "track" in item else item
        if not track:
            continue
        url = _resolve_track_url(track)
        if url:
            urls.append(url)
    logger.info("Collected %d track URLs from album", len(urls))
    return urls


# ═══════════════════════════════════════════════════════════════════════════════
# Per-track fetcher  (playlist / album loop)
# ═══════════════════════════════════════════════════════════════════════════════

async def _fetch_one_track(
    client: SpotifyClient,
    track_url: str,
    idx: int,
    total: int,
    seen: set[str],
    results: list[dict],
    progress_msg=None,
    task_id: Optional[str] = None,
    running_tasks: Optional[dict] = None,
) -> None:
    """Fetch metadata for one track URL and append to results."""
    if track_url in seen:
        logger.debug("Duplicate skipped: %s", track_url)
        return

    running_tasks = running_tasks or {}
    if task_id and running_tasks.get(task_id, {}).get("cancelling"):
        logger.info("Task %s cancelling — stopping fetch loop", task_id)
        return

    try:
        raw = await asyncio.get_event_loop().run_in_executor(
            None, client.get_track_info, track_url
        )
    except Exception as exc:
        logger.warning("[%d/%d] Failed to fetch %s: %s", idx, total, track_url, exc)
        return

    track_info = _parse_track(raw, override_url=track_url)

    if _invalid(track_info):
        logger.warning("[%d/%d] Invalid data — skipping %s", idx, total, track_url)
        return

    seen.add(track_url)
    results.append(track_info)
    logger.debug("[%d/%d] ✓ %s — %s", idx, total, track_info["title"], track_info["artists"])

    if progress_msg:
        await update_progress(
            progress_msg,
            f"🎵 Fetching tracks… {len(results)}/{total}\n"
            f"<i>{track_info['title']} — {track_info['artists']}</i>",
            task_id=task_id,
            running_tasks=running_tasks,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════════

async def extract_spotify_tracks(
    url: str,
    vareon_id: str,
    progress_msg=None,
    task_id: Optional[str] = None,
    running_tasks: Optional[dict] = None,
) -> list[dict]:
    """
    Main entry point called by the Telegram bot handler.

    Parameters
    ----------
    url              : Spotify track / album / playlist URL
    user_id          : Integer Telegram user ID (used to find cookie files)
    progress_msg : Telegram Message object for live status updates
    task_id          : Key in running_tasks for cancellation support
    running_tasks    : Shared task-state dict

    Returns
    -------
    List of enriched track dicts:
        url · title · artists · writers · album · release_date ·
        duration · duration_ms · coverUrl · music_link
    """
    running_tasks = running_tasks or {}
    seen:    set[str]  = set()
    results: list[dict] = []

    logger.info("=== Starting extraction: %s ===", url)

    # ── Spotify client ──────────────────────────────────────────────────────
    client = _make_client(vareon_id)

    # Initial status
    if progress_msg:
        await update_progress(
            progress_msg,
            "🎵 Connecting to Spotify…\n\n📡 Fetching track details…",
            task_id=task_id,
            running_tasks=running_tasks,
        )

    try:
        # ── SINGLE TRACK ────────────────────────────────────────────────────
        if "/track/" in url:
            logger.info("Detected: single track")

            raw = await asyncio.get_event_loop().run_in_executor(
                None, client.get_track_info, url
            )

            track_info = _parse_track(raw, override_url=url)

            if _invalid(track_info):
                logger.error("Invalid Spotify data for: %s", url)
                if progress_msg:
                    await update_progress(
                        progress_msg,
                        "❌ Could not fetch track info from Spotify.\nPlease retry.",
                        remove_keyboard=True,
                        task_id=task_id,
                        running_tasks=running_tasks,
                    )
                return []

            results.append(track_info)

            title   = track_info.get("title", "—")
            artist  = track_info.get("artists", "—")
            album   = track_info.get("album") or "—"
            dur     = track_info.get("duration") or "—"
            rel     = track_info.get("release_date") or "—"
            writers = track_info.get("writers")
            sp_url  = track_info.get("url")

            logger.info("Single track fetched: %s — %s", title, artist)

            # ── User-facing rich info ────────────────────────────────────────
            if progress_msg:
                msg_text = (
                    "🎧 <b>Track Found</b>\n\n"
                    f"🎵 <b>Title:</b> {title}\n"
                    f"👤 <b>Artist:</b> {artist}\n"
                    f"💿 <b>Album:</b> {album}\n"
                    f"⏱ <b>Duration:</b> {dur}\n"
                    f"📅 <b>Release:</b> {rel}\n"
                )

                if writers:
                    msg_text += f"✍️ <b>Writers:</b> {writers}\n"

                if sp_url:
                    msg_text += f"\n🔗 <a href='{sp_url}'>Open on Spotify</a>\n"

                await update_progress(
                    progress_msg,
                    msg_text,
                    task_id=task_id,
                    running_tasks=running_tasks,
                )
                
        # ── PLAYLIST ────────────────────────────────────────────────────────
        elif "/playlist/" in url:
            logger.info("Detected: playlist")
            if progress_msg:
                await update_progress(
                    progress_msg,
                    "📋 Fetching playlist info…",
                    task_id=task_id,
                    running_tasks=running_tasks,
                )

            data = await asyncio.get_event_loop().run_in_executor(
                None, client.get_playlist_info, url
            )
            playlist_name = data.get("name") or "Playlist"
            track_urls    = _collect_urls_from_playlist(data)
            total         = len(track_urls)

            if not track_urls:
                if progress_msg:
                    await update_progress(
                        progress_msg,
                        "❌ No tracks found in this playlist.",
                        remove_keyboard=True,
                        task_id=task_id,
                        running_tasks=running_tasks,
                    )
                return []

            logger.info("Playlist '%s': %d tracks to fetch", playlist_name, total)
            if progress_msg:
                await update_progress(
                    progress_msg,
                    f"📋 <b>{playlist_name}</b>\n"
                    f"Found {total} tracks — fetching metadata…",
                    task_id=task_id,
                    running_tasks=running_tasks,
                )

            for idx, track_url in enumerate(track_urls, 1):
                if task_id and running_tasks.get(task_id, {}).get("cancelling"):
                    break

                await _fetch_one_track(
                    client, track_url, idx, total,
                    seen, results,
                    progress_msg, task_id, running_tasks,
                )

                # Human-like pacing
                await _human_delay(0.6, 1.8)
                await _burst_delay(idx)

        # ── ALBUM ────────────────────────────────────────────────────────────
        elif "/album/" in url:
            logger.info("Detected: album")
            if progress_msg:
                await update_progress(
                    progress_msg,
                    "💿 Fetching album info…",
                    task_id=task_id,
                    running_tasks=running_tasks,
                )

            data = await asyncio.get_event_loop().run_in_executor(
                None, client.get_album_info, url
            )
            album_name = data.get("name") or "Album"
            track_urls = _collect_urls_from_album(data)
            total      = len(track_urls)

            if not track_urls:
                if progress_msg:
                    await update_progress(
                        progress_msg,
                        "❌ No tracks found in this album.",
                        remove_keyboard=True,
                        task_id=task_id,
                        running_tasks=running_tasks,
                    )
                return []

            logger.info("Album '%s': %d tracks to fetch", album_name, total)
            if progress_msg:
                await update_progress(
                    progress_msg,
                    f"💿 <b>{album_name}</b>\n"
                    f"Found {total} tracks — fetching metadata…",
                    task_id=task_id,
                    running_tasks=running_tasks,
                )

            for idx, track_url in enumerate(track_urls, 1):
                if task_id and running_tasks.get(task_id, {}).get("cancelling"):
                    break

                await _fetch_one_track(
                    client, track_url, idx, total,
                    seen, results,
                    progress_msg, task_id, running_tasks,
                )

                await _human_delay(0.6, 1.8)
                await _burst_delay(idx)

        else:
            logger.error("Unsupported URL type: %s", url)
            if progress_msg:
                await update_progress(
                    progress_msg,
                    "❌ Unsupported link. Please send a Spotify track, album, or playlist URL.",
                    remove_keyboard=True,
                    task_id=task_id,
                    running_tasks=running_tasks,
                )
            return []

    except Exception as exc:
        logger.error("Extraction failed for %s: %s", url, exc, exc_info=True)
        if progress_msg:
            await update_progress(
                progress_msg,
                "❌ Spotify extraction failed. Please retry.",
                remove_keyboard=True,
                task_id=task_id,
                running_tasks=running_tasks,
            )
        raise

    logger.info("Extraction complete: %d tracks collected", len(results))

    if not results:
        return []

    # ── YouTube matching ──────────────────────────────────────────────────────
    await _match_youtube(
        results,
        progress_msg=progress_msg,
        task_id=task_id,
        running_tasks=running_tasks,
    )

    logger.info("YouTube matching complete")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# YouTube matching loop
# ═══════════════════════════════════════════════════════════════════════════════

async def _match_youtube(
    tracks: list[dict],
    progress_msg=None,
    task_id: Optional[str] = None,
    running_tasks: Optional[dict] = None,
) -> None:
    """
    Enrich every track dict with a "music_link" key (YouTube watch URL or None).
    Calls the existing find_best_youtube_match with the correct signature.
    """
    running_tasks = running_tasks or {}
    total = len(tracks)

    already = sum(1 for t in tracks if t.get("music_link"))
    to_do   = [t for t in tracks if not t.get("music_link")]

    if not to_do:
        logger.info("All tracks already have music_link — skipping YouTube search")
        return

    logger.info("[YT] Matching %d tracks (%d already done)", len(to_do), already)

    newly_matched = 0
    for idx, track in enumerate(to_do, 1):
        global_idx = tracks.index(track) + 1

        if task_id and running_tasks.get(task_id, {}).get("cancelling"):
            logger.info("Task %s cancelling — stopping YouTube matching", task_id)
            break

        title   = track.get("title",   "Unknown Title")
        artists = track.get("artists", "Unknown Artist")

        try:
            yt_url = await find_best_youtube_match(
                track=track,
                progress_msg=progress_msg,
                idx=global_idx,
                total=total,
                task_id=task_id,
            )
        except Exception as exc:
            logger.error("YouTube match error for '%s': %s", title, exc)
            yt_url = None

        track["music_link"] = yt_url
        if yt_url:
            newly_matched += 1
            logger.info("[YT] [%d/%d] ✓ %s", global_idx, total, yt_url)
        else:
            logger.warning("[YT] [%d/%d] No match for: %s", global_idx, total, title)

    final = already + newly_matched
    logger.info("[YT] Done: %d/%d tracks matched (%d new)", final, total, newly_matched)

# ═══════════════════════════════════════════════════════════════════════════════
# YouTube search + best-match (Playwright — unchanged logic, cleaned up)
# ═══════════════════════════════════════════════════════════════════════════════

def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (text or "").lower()).strip()

def _duration_to_seconds(s: str | None) -> int | None:
    if not s:
        return None
    parts = s.strip().split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


async def find_best_youtube_match(
    track: dict,
    progress_msg=None,
    idx: int = 0,
    total: int = 0,
    task_id=None,
) -> str | None:

    title = track.get("title", "Unknown Title")
    artist = track.get("artists", "Unknown Artist")
    duration_str = track.get("duration")

    logger.info("[YTMUSIC] Searching: %s — %s", title, artist)

    clean_title = _normalize(title)
    clean_artist = _normalize(artist)
    target_sec = _duration_to_seconds(duration_str)

    query = f"{clean_title} {clean_artist}"
    logger.info("[YTMUSIC] Query: %s", query)

    try:
        # ─────────────────────────────────────────────
        # 1. Fetch YouTube Music results (songs only)
        # ─────────────────────────────────────────────
        results = ytmusic.search(query, filter="songs", limit=10)

        if not results:
            logger.warning("[YTMUSIC] No results for %s", query)
            return None

        BAD_WORDS = {
            "reverb", "cover",
            "live", "karaoke", "extended",
            "nightcore"
        }

        best_score = -1
        selected_id = None

        norm_title = _normalize(clean_title)
        artist_tokens = clean_artist.split()

        for r in results:
            title_r = _normalize(r.get("title", ""))
            artists_r = r.get("artists", [])
            artist_r = _normalize(
                " ".join([a.get("name", "") for a in artists_r]) if artists_r else ""
            )

            full_text = f"{title_r} {artist_r}"

            # ❌ filter bad versions
            if any(bw in full_text for bw in BAD_WORDS):
                continue

            score = 0

            # 🎯 title similarity (strong signal)
            score += fuzz.token_set_ratio(norm_title, title_r) * 0.6

            # 🎤 artist similarity
            score += fuzz.token_set_ratio(clean_artist, artist_r) * 0.3

            # 🎧 exact token match bonus
            score += sum(10 for t in artist_tokens if t in artist_r)

            # ⏱ duration match (if available)
            yt_duration = r.get("duration_seconds") or None
            if target_sec and yt_duration:
                diff = abs(target_sec - yt_duration)
                if diff <= 2:
                    score += 40
                elif diff <= 5:
                    score += 25
                elif diff <= 10:
                    score += 10
                else:
                    score -= 15

            # 🎵 official boost
            if r.get("resultType") == "song":
                score += 10

            if score > best_score:
                best_score = score
                selected_id = r.get("videoId")

        if not selected_id:
            logger.warning("[YTMUSIC] No good match found for %s", query)
            return None

        url = f"https://www.youtube.com/watch?v={selected_id}"
        logger.info("[YTMUSIC] Selected %s (score=%.2f)", url, best_score)

        return url

    except Exception as e:
        logger.error("[YTMUSIC] Search failed: %s", e, exc_info=True)
        return None