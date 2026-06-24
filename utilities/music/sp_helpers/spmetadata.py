import asyncio
import aiohttp
import os
import time, os, state
from main.config import logger
from utilities.music.sp_helpers.splyrics import SpotifyClient
from utilities.music.utils import extract_track_id, upload_song_to_telegram
_ffmpeg_semaphore = asyncio.Semaphore(1)

async def apply_spotify_postprocess(
    idx: int,
    final_path: str,
    title: str,
    artist: str,
    album: str,
    spotify_track_url: str,
    music_link: str,
    cover_url: str,
    target: str,
    context,
    update,
    progress_msg,
    sp_dc_value: str = None
):
    """
    Runs metadata + cover + lyrics + upload for one track.
    ffmpeg ops are serialized via semaphore (no concurrent heavy CPU),
    but this whole function runs as an asyncio.create_task so the
    main loop can start downloading the next track immediately.
    """
    
    # ── Initial status ───────────────────────────────────────────────
    async def update_progress_msg(text):
        try:
            return await progress_msg.reply_text(text, parse_mode="HTML")
        except Exception:
            return None
    upload_msg = await update_progress_msg(
        "🔄 <b>Processing Complete</b>\n"
        "\n"
        "<i>Performing post-processing tasks:</i>\n"
        "‼️  <b>• Embedding Title & Artist & Album 🎵</b>\n"
        "• Adding Thumbnail 🖼️\n"
        "• Syncing Lyrics ✍️"
    )
    
    async with _ffmpeg_semaphore:
        # ── Metadata ────────────────────────────────────────────────
        temp_meta = final_path + ".meta.mp3"
        youtube_url = music_link if music_link.startswith("https://www.youtube.com") else ""

        meta_cmd = [
            "ffmpeg", "-y",
            "-i", final_path,
            "-map", "0",
            "-c", "copy",
            "-metadata", f"title={title}",
            "-metadata", f"artist={artist}",
            "-metadata", f"album={album or ''}",
            "-metadata", f"purl={spotify_track_url}",
            "-metadata", f"url={youtube_url}",
            "-metadata", f"comment=Spotify: {spotify_track_url}\nYouTube: {youtube_url}",
            temp_meta
        ]

        proc = await asyncio.create_subprocess_exec(
            *meta_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()

        if proc.returncode == 0 and os.path.exists(temp_meta):
            os.replace(temp_meta, final_path)
            logger.info(f"[{idx}] Spotify metadata applied")
        else:
            logger.warning(f"[{idx}] Metadata step failed (rc={proc.returncode})")

        await upload_msg.edit_text(
            text=(
            "🔄 <b>Processing Complete</b>\n"
            "\n"
            "<i>Performing post-processing tasks:</i>\n"
            "✅  • Embedding Title & Artist & Album 🎵\n"
            "‼️  <b>• Adding Thumbnail 🖼️</b>\n"
            "• Syncing Lyrics ✍️\n"
            ),
            parse_mode="HTML"
        )
        # ── Cover embed ─────────────────────────────────────────────
        if cover_url and isinstance(cover_url, str):
            cleaned_cover = cover_url.strip()
            if cleaned_cover.startswith('url('):
                cleaned_cover = cleaned_cover[4:].rstrip(')"\'').strip('"\'')
            if cleaned_cover.startswith(('http://', 'https://')):
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(cleaned_cover, timeout=12) as resp:
                            if resp.status == 200:
                                temp_jpg = final_path + ".jpg"
                                temp_mp3 = final_path + ".tmp.mp3"

                                content = await resp.read()
                                with open(temp_jpg, "wb") as f:
                                    f.write(content)

                                ffmpeg_cmd = [
                                    "ffmpeg", "-y",
                                    "-i", final_path,
                                    "-i", temp_jpg,
                                    "-map", "0:a",
                                    "-map", "1:v",
                                    "-c:a", "copy",
                                    "-c:v", "mjpeg",
                                    "-disposition:v:0", "attached_pic",
                                    "-metadata:s:v", "title=Cover",
                                    temp_mp3
                                ]

                                proc = await asyncio.create_subprocess_exec(
                                    *ffmpeg_cmd,
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE
                                )
                                _, stderr_bytes = await proc.communicate()

                                if proc.returncode == 0:
                                    os.replace(temp_mp3, final_path)
                                    logger.info(f"[{idx}] Spotify cover embedded")
                                    await upload_msg.edit_text(
                                        text=("🔄 <b>Processing Complete</b>\n"
                                        "\n"
                                        "<i>Performing post-processing tasks:</i>\n"
                                        "✅  • Embedding Title & Artist & Album 🎵\n"
                                        "✅  • Adding Thumbnail 🖼️\n"
                                        "‼️  <b>• Syncing Lyrics ✍️</b>\n"
                                        ),
                                        parse_mode="HTML"
                                    )
                                else:
                                    logger.warning(f"[{idx}] Cover embed failed (rc={proc.returncode})")

                                for tmp in (temp_jpg, temp_mp3):
                                    if os.path.exists(tmp):
                                        try:
                                            os.remove(tmp)
                                        except:
                                            pass
                except Exception as e:
                    logger.warning(f"[{idx}] Cover download/embed failed: {e}")

        # ── Lyrics ──────────────────────────────────────────────────
        if spotify_track_url and sp_dc_value:
            try:
                logger.info(f"[{idx}] Fetching & embedding synced lyrics with mutagen (SYLT)")
                client = SpotifyClient(sp_dc_value)
                track_id = extract_track_id(spotify_track_url)
                lyrics_data = client.get_lyrics(track_id)

                if lyrics_data and "lyrics" in lyrics_data:
                    lyr = lyrics_data["lyrics"]
                    lines = lyr.get("lines", [])
                    is_synced = lyr.get("syncType") == "LINE_SYNCED"

                    if not is_synced:
                        logger.info(f"[{idx}] Lyrics are unsynced → skipping SYLT embed")
                    else:
                        sylt_entries = [
                            (line.get("words", "").strip(), int(line.get("startTimeMs", 0)))
                            for line in lines if line.get("words", "").strip()
                        ]

                        if sylt_entries:
                            from mutagen.id3 import ID3, SYLT, Encoding
                            audio = ID3(final_path)
                            audio.delall("SYLT")
                            audio.delall("USLT")
                            audio.add(SYLT(
                                encoding=Encoding.UTF8,
                                lang="eng",
                                format=2,
                                type=1,
                                text=sylt_entries
                            ))
                            audio.save()
                            logger.info(f"[{idx}] Synced lyrics embedded successfully (SYLT tag)")
                        else:
                            logger.info(f"[{idx}] No valid timed lines found")
                else:
                    logger.info(f"[{idx}] No lyrics data from Spotify")
                await upload_msg.edit_text(
                    text=("🔄 <b>Processing Complete</b>\n"
                    "\n"
                    "<i>Performing post-processing tasks:</i>\n"
                    "✅  • Embedding Title & Artist & Album 🎵\n"
                    "✅  • Adding Thumbnail 🖼️\n"
                    "✅  • Syncing Lyrics ✍️\n"
                    ),
                    parse_mode="HTML"
                )
            except ImportError as e:
                logger.error(f"[{idx}] mutagen not installed: {e}")
            except Exception as e:
                logger.error(f"[{idx}] Lyrics embed failed: {str(e)}")
                
    # ── Upload — OUTSIDE semaphore, no reason to hold it ────────────
    if target == "music_on_telegram":
        await upload_song_to_telegram(
            file_path=final_path,
            title=title,
            artist=artist,
            user_id=update.effective_user.id,
            upload_msg=upload_msg,
            context=context,
        )
