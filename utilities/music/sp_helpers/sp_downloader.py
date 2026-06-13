import asyncio
import os
from telegram import Update
from telegram.ext import ContextTypes
from typing import Optional
from main.config import logger, USERS_PATH, COOKIES_PATH
from utilities.music.sp_helpers.sptrack import extract_spotify_tracks
from utilities.music.sp_helpers.spmetadata import apply_spotify_postprocess
from http.cookiejar import MozillaCookieJar
from utilities.music.utils import run_download, clean_filename, upload_song_to_telegram, update_progress 

async def scrape_spotify_to_youtube(
    spotify_url,
    download_path,
    progress_msg,
    vareon_id,
    task_id,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    target: str = None,
    running_tasks: Optional[dict] = None,
) -> list[str]:
    """
    Full pipeline inside one function:
    1. Extract tracks from Spotify URL
    2. Enrich with best YouTube matches (adds "music_link")
    3. Download each track using yt-dlp + embed Spotify cover
    """
    logger.info(f"[SPOTIFY→YT] Starting full process for: {spotify_url}")
    spotify_cookie_path = COOKIES_PATH / "spotify" / f"{vareon_id}.txt"
    # 🎵 SPOTIFY FLOW        
    if not os.path.exists(spotify_cookie_path):
            await progress_msg.reply_text(
                "🍪**Cookies Required**\n\n"
                "To fetch formats and ensure high-speed downloads, please set your **Spotify** cookies first:\n\n"
                "1. Run the command /cookies\n"
                "2. Follow the instructions to upload your `.txt` cookie file.\n"
                "3. Once done, come back here and try again!\n\n",
                parse_mode="Markdown"
                )
            return
        
    # ─── Step 1: Extract Spotify tracks ───────────────────────────────
    tracks = await extract_spotify_tracks(
        url=spotify_url,
        vareon_id=vareon_id,
        progress_msg=progress_msg,
        task_id=task_id,
        running_tasks=running_tasks,
    )
    logger.info(f"[SPOTIFY] Extracted {len(tracks)} tracks with metadata")

    if not tracks:
        await update_progress(progress_msg,"❌ No tracks found in the Spotify link.",remove_keyboard=True, task_id=task_id)
        return []

    # ─── Step 2: Download phase ───────────────────────────────────────

    final_link = []
    total_tracks = len(tracks)

    for idx, track in enumerate(tracks, start=1):
        title     = track.get("title", "Unknown Title")
        artist    = track.get("artists", "Unknown Artist")
        album     = track.get("album", None)
        music_link = track.get("music_link")
        cover_url = track.get("coverUrl")
        spotify_track_url = track.get("url") or spotify_url

        if not music_link:
            logger.warning(f"[{idx}] No music_link → skipping")
            continue

        safe_title  = clean_filename(title)
        safe_artist = clean_filename(artist)
        filename    = f"{safe_title} - {safe_artist}.mp3"

        if target == "music_on_telegram":
            temp_path  = os.path.abspath(f"{USERS_PATH}/{vareon_id}/.temp")
            os.makedirs(temp_path, exist_ok=True)
            final_path = os.path.join(temp_path, filename)
        else:
            final_path = os.path.join(download_path, filename)

        # Skip if already exists
        if os.path.exists(final_path):
            if target == "music_on_telegram":
                await upload_song_to_telegram(
                    chat_id=update.effective_chat.id,
                    file_path=final_path,
                    title=title,
                    artist=artist,
                    user_id=update.effective_user.id,
                    upload_msg=None,
                    context=context,
                )
            else:
                logger.info(f"[{idx}] File already exists → skipping")
            final_link.append(music_link)
            continue

        await update_progress(progress_msg,
            f"🎵 Downloading: [{idx}/{total_tracks}] \n<i>{title} - {artist}</i>",
            task_id=task_id,
            running_tasks=running_tasks,
        )

        downloaded = False

        # ── CASE 1: YouTube ─────────────────────────────────────────────────
        if not downloaded and music_link.startswith("https://www.youtube.com/watch?v="):
            output_template = final_path[:-4]
            youtube_cookie_path = COOKIES_PATH / "youtube" / f"{vareon_id}.txt"
            cookie_args = ["--cookies", str(youtube_cookie_path)] if youtube_cookie_path.exists() else []

            cmd = [
                "yt-dlp",
                "-f", "bestaudio/best",
                "--extract-audio",
                "--audio-format", "mp3",
                "--audio-quality", "0",
                "--no-overwrites",
                "--js-runtimes", "node",
                "--remote-components", "ejs:github",
                *cookie_args,
                "-o", f"{output_template}.%(ext)s",
                music_link
            ]

            rc, err = await run_download(cmd, idx, total_tracks, progress_msg, task_id)

            if os.path.exists(final_path):
                downloaded = True
                logger.info(f"[{idx}] YouTube download OK (file present)")
            else:
                logger.error(f"[{idx}] YouTube download failed - file missing (rc={rc}, error={err})")
                
        # ── If nothing downloaded → skip to next track ──────────────────────────
        if not downloaded or not os.path.exists(final_path):
            await update_progress(
                progress_msg,
                f"[{idx}/{total_tracks}] Download failed: {title}",
                task_id=task_id,
                running_tasks=running_tasks,
            )
            continue

        # ── SUCCESS: Now apply Spotify metadata + cover + lyrics ────────────────
        if downloaded:
            sp_dc_value = None
            spotify_cookie_path = COOKIES_PATH / "spotify" / f"{vareon_id}.txt"
            if os.path.exists(spotify_cookie_path):
                try:
                    jar = MozillaCookieJar(str(spotify_cookie_path))
                    jar.load(ignore_discard=True, ignore_expires=True)
                    for cookie in jar:
                        if cookie.name == "sp_dc":
                            sp_dc_value = cookie.value
                            break
                except Exception as e:
                    logger.error(f"[{idx}] Failed to load cookie: {e}")

            asyncio.create_task(apply_spotify_postprocess(
                idx=idx,
                final_path=final_path,
                title=title,
                artist=artist,
                album=album,
                spotify_track_url=spotify_track_url,
                music_link=music_link,
                cover_url=cover_url,
                target=target,
                context=context,
                update=update,
                progress_msg=progress_msg,
                sp_dc_value=sp_dc_value
            ))
            final_link.append(music_link)        

    # Final message (after loop)
    count = len(final_link)
    return final_link
