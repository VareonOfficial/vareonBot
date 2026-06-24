import asyncio
from ytmusicapi import YTMusic
from mutagen.id3 import ID3, USLT, ID3NoHeaderError
from main.config import logger
from utilities.music.utils import upload_song_to_telegram


async def apply_youtube_postprocess(
    final_path: str,
    video_id: str,
    title: str,
    artist: str,
    context,
    update,
):
    """
    Fetches lyrics from YTMusic and injects them as a USLT ID3 frame
    into the already-tagged MP3 (yt-dlp handles all other metadata).
    Then uploads the file to Telegram.

    Designed to be called as an asyncio.create_task().
    """
    try:
        loop = asyncio.get_event_loop()
        lyrics_text = await loop.run_in_executor(None, _fetch_yt_lyrics, video_id)

        if lyrics_text:
            await loop.run_in_executor(None, _write_lyrics_tag, final_path, lyrics_text)
            logger.info("[YT POST] Lyrics written for: %s", final_path)
        else:
            logger.info("[YT POST] No lyrics found for video_id=%s", video_id)

    except Exception as e:
        logger.error("[YT POST] Lyrics postprocess failed for %s: %s", final_path, e)

    try:
        await upload_song_to_telegram(
            file_path=final_path,
            title=title,
            artist=artist,
            user_id=(update.effective_user.id if update.effective_user else None),
            upload_msg=None,
            context=context,
        )
    except Exception as e:
        logger.error("[YT POST] Upload failed for %s: %s", final_path, e)


def _fetch_yt_lyrics(video_id: str) -> str | None:
    """Synchronous — always call via run_in_executor."""
    try:
        yt = YTMusic()
        watch = yt.get_watch_playlist(videoId=video_id)
        lyrics_id = watch.get("lyrics")
        if lyrics_id:
            data = yt.get_lyrics(lyrics_id)
            return (data or {}).get("lyrics")
    except Exception as e:
        logger.warning("[YT POST] YTMusic lyrics fetch failed for %s: %s", video_id, e)
    return None


def _write_lyrics_tag(file_path: str, lyrics: str) -> None:
    """Synchronous — always call via run_in_executor."""
    try:
        tags = ID3(file_path)
    except ID3NoHeaderError:
        tags = ID3()

    tags["USLT::eng"] = USLT(
        encoding=3,
        lang="eng",
        desc="",
        text=lyrics,
    )
    tags.save(file_path, v2_version=3)