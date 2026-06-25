from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
import asyncio
from features.music.sp_helpers.sptrack import _make_client, _collect_urls_from_playlist, _collect_urls_from_album
from ytmusicapi import YTMusic

ytmusic = YTMusic()

async def is_link_size_valid(link: str, vareon_id: str) -> bool:
    """
    Checks if a Spotify or YouTube Music link contains 50 or fewer tracks.
    """
    loop = asyncio.get_event_loop()

    # --- Spotify Logic ---
    if "open.spotify.com" in link:
        try:
            client = _make_client(vareon_id) # Authenticates via cookie files
            
            if "/playlist/" in link:
                data = await loop.run_in_executor(None, client.get_playlist_info, link)
                track_urls = _collect_urls_from_playlist(data) #
                return 0 < len(track_urls) <= 50
                
            elif "/album/" in link:
                data = await loop.run_in_executor(None, client.get_album_info, link)
                track_urls = _collect_urls_from_album(data) #[cite: 1]
                return 0 < len(track_urls) <= 50
                
            return True # Individual tracks pass automatically

        except Exception:
            return False

    # --- YouTube Music Logic ---
    elif "list=" in link:
        try:
            playlist_id = link.split("list=")[1].split("&")[0]
            # YTMusicAPI is generally synchronous; run in thread to avoid blocking
            playlist_details = await loop.run_in_executor(None, ytmusic.get_playlist, playlist_id)
            track_count = int(playlist_details.get('trackCount', 0))
            return 0 < track_count <= 50
        except Exception:
            return False

    return True # Default for other links

async def show_upload_option(context, chat_id, link, vareon_id):
    # 1. Perform the size check first
    is_valid = await is_link_size_valid(link, vareon_id)
    
    # 2. If it exceeds 50, end the function silently
    if not is_valid:
        return None

    # 3. Otherwise, show the message and button
    keyboard = [
        [InlineKeyboardButton("📤 Upload to Telegram", callback_data="download_here_tg", style="primary")]
    ]
    text = (
        "🎵 **Upload to Telegram**\n\n"
        "⚠️ **Limitations:**\n"
        "• Playlists with more than 50 songs cannot be uploaded in a single request.\n"
        "• Very large files may take additional time depending on processing and upload speed.\n\n"
    )

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return msg