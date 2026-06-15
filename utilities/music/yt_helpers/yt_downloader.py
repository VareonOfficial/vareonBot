import asyncio
import os
from typing import Optional
from main.config import logger, USERS_PATH, COOKIES_PATH
from utilities.music.utils import run_download, update_progress 
from utilities.music.yt_helpers.ytmetadata import apply_youtube_postprocess
from vareon_analytics.vr_log import log_to_db

async def scrape_youtube_to_download(
    link,
    download_path,
    progress_msg,
    vareon_id,
    task_id,
    update,
    context,
    target,
    running_tasks: Optional[dict] = None,
):
    running_tasks = running_tasks or {}
    youtube_cookie_path = COOKIES_PATH / "youtube" / f"{vareon_id}.txt"
    if target == "music_on_telegram":
        temp_path = os.path.abspath(f"{USERS_PATH}/{vareon_id}/.tmp")
        os.makedirs(temp_path, exist_ok=True)
        download_path = temp_path
    else:
        os.makedirs(download_path, exist_ok=True)

    is_playlist = "playlist" in link or "list=" in link

    cookie_args = []
    if youtube_cookie_path.exists():
        cookie_args = ["--cookies", str(youtube_cookie_path)]
        logger.info("[MUSIC YT] Using cookie: %s", youtube_cookie_path)
    else:
        logger.info("[YT-DLP] No cookie file found for vareon_id=%s. Prompting user.", vareon_id)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "🍪 **Cookies Required**\n\n"
                "To fetch formats and ensure high-speed downloads, please set your **YouTube** cookies first:\n\n"
                "1. Run the command /cookies\n"
                "2. Follow the instructions to upload your `.txt` cookie file.\n"
                "3. Once done, come back here and try again!\n\n"
            ),
            parse_mode="Markdown"
        )
        return

    base_cmd = [
        "yt-dlp",
        "-f", "bestaudio/best",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--add-metadata",
        "--embed-thumbnail",
        "--no-write-thumbnail",
        "--no-overwrites",
        "--ignore-errors",
        "--no-abort-on-error",
        "--js-runtimes", "node",
        "--remote-components", "ejs:github",
        *cookie_args,
        "-o", os.path.join(download_path, f"{task_id}_%(title)s.%(ext)s"),
    ]

    final_links = []
    if is_playlist:
        await update_progress(progress_msg, "📃 Playlist detected...", task_id=task_id, running_tasks=running_tasks)

        count_cmd = [
            "yt-dlp", "--flat-playlist", "--print", "id",
            "--js-runtimes", "node",
            "--remote-components", "ejs:github",
            *cookie_args,
            link
        ]
        proc = await asyncio.create_subprocess_exec(
            *count_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        out, _ = await proc.communicate()
        ids = out.decode().strip().splitlines()
        total = len(ids)
        
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=update.effective_user.id,
            event_type="MUSIC_INFO",
            function_name="scrape_youtube_to_download",
            task_id=task_id,
            details={
                "path": download_path,
                "url": link,
                "type": "youtube_playlist",
                "num_tracks": total,
            },
            action_status={"status": "in_progress"},
        )


        if total == 0:
            await update_progress(progress_msg, "❌ Playlist empty.", remove_keyboard=True, task_id=task_id, running_tasks=running_tasks)
            return final_links

        completed = 0
        tasks_fired = 0
        for idx, vid in enumerate(ids, 1):
            if running_tasks.get(task_id, {}).get("cancelling"):
                break

            video_url = f"https://www.youtube.com/watch?v={vid}"
            cmd = base_cmd + [video_url]

            rc, err = await run_download(
                cmd, idx, total, progress_msg, task_id,
                vareon_id=vareon_id,
                tg_user_id=update.effective_user.id,
                tracks_completed=completed,
            )

            if rc == 0:
                completed += 1

            if rc != 0:
                continue

            if target == "music_on_telegram":
                files = [f for f in os.listdir(temp_path) if f.startswith(f"{task_id}_") and f.endswith(".mp3")]

                if not files:
                    await update_progress(progress_msg, "❌ File not found after download.", remove_keyboard=True, task_id=task_id, running_tasks=running_tasks)
                    continue

                files.sort(key=lambda f: os.path.getctime(os.path.join(temp_path, f)), reverse=True)
                filename = files[0]
                final_path = os.path.join(temp_path, filename)

                name = os.path.splitext(filename[len(f"{task_id}_"):])[0]
                parts = name.split(" - ", 1)
                title  = parts[0].strip() if len(parts) > 0 else name
                artist = parts[1].strip() if len(parts) > 1 else "Unknown Artist"

                final_links.append(final_path)

                asyncio.create_task(
                    apply_youtube_postprocess(
                        final_path=final_path,
                        video_id=vid,
                        title=title,
                        artist=artist,
                        context=context,
                        update=update,
                    )
                )
                tasks_fired += 1
            else:
                files = [f for f in os.listdir(download_path) if f.startswith(f"{task_id}_") and f.endswith(".mp3")]
                files.sort(key=lambda f: os.path.getctime(os.path.join(download_path, f)), reverse=True)
                for f in files:
                    old_path = os.path.join(download_path, f)
                    new_path = os.path.join(download_path, f[len(f"{task_id}_"):])
                    os.rename(old_path, new_path)
                    final_links.append(new_path)
                    
        # ── After loop, one single log ──────────────────────────────────
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=update.effective_user.id,
            event_type="DOWNLOAD_SESSION_COMPLETE",
            function_name="scrape_youtube_to_download",
            task_id=task_id,
            details={
                "tracks_completed": completed,
                "tracks_in_total": total,
                "type": "youtube_playlist",
            },
            action_status={"status": "success"},
        )
        if tasks_fired > 0:
            log_to_db(
                vareon_id=vareon_id,
                tg_user_id=update.effective_user.id,
                event_type="UPLOAD_SESSION_FIRED",
                function_name="scrape_youtube_to_download",
                task_id=task_id,
                details={
                    "tracks_fired":    tasks_fired,
                    "tracks_in_total": total,
                },
                action_status={"status": "dispatched"},
            )
        

    else:
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=update.effective_user.id,
            event_type="MUSIC_INFO",
            function_name="scrape_youtube_to_download",
            task_id=task_id,
            details={
                "path": download_path,
                "url": link,
                "type": "youtube_single",
                "num_tracks": "1",
            },
            action_status={"status": "in_progress"},
        )
        cmd = base_cmd + [link]
        rc, err = await run_download(
            cmd, 1, 1, progress_msg, task_id,
            vareon_id=vareon_id,
            tg_user_id=update.effective_user.id,)

        if rc == 0:
            log_to_db(
                vareon_id=vareon_id,
                tg_user_id=update.effective_user.id,
                event_type="DOWNLOAD_COMPLETE",
                function_name="scrape_youtube_to_download",
                task_id=task_id,
                details={
                    "type": "youtube_single",
                },
                action_status={"status": "success"},
            )
            if target == "music_on_telegram":
                files = [f for f in os.listdir(temp_path) if f.startswith(f"{task_id}_") and f.endswith(".mp3")]

                if not files:
                    await update_progress(progress_msg, "❌ File not found after download.", remove_keyboard=True, task_id=task_id, running_tasks=running_tasks)
                    return final_links

                filename = files[0]
                final_path = os.path.join(temp_path, filename)

                name = os.path.splitext(filename[len(f"{task_id}_"):])[0]
                parts = name.split(" - ", 1)
                title  = parts[0].strip() if len(parts) > 0 else name
                artist = parts[1].strip() if len(parts) > 1 else "Unknown Artist"

                final_links.append(final_path)

                await apply_youtube_postprocess(
                    final_path=final_path,
                    video_id=link.split("v=")[-1].split("&")[0],
                    title=title,
                    artist=artist,
                    context=context,
                    update=update,
                )

                log_to_db(
                    vareon_id=vareon_id,
                    tg_user_id=update.effective_user.id,
                    event_type="UPLOAD_SESSION_COMPLETE",
                    function_name="scrape_youtube_to_download",
                    task_id=task_id,
                    details={
                        "tracks_uploaded": 1,
                        "type": "youtube_single",
                    },
                    action_status={"status": "success"},
                )
            else:
                files = [f for f in os.listdir(download_path) if f.startswith(f"{task_id}_") and f.endswith(".mp3")]
                for f in files:
                    old_path = os.path.join(download_path, f)
                    new_path = os.path.join(download_path, f[len(f"{task_id}_"):])
                    os.rename(old_path, new_path)
                    final_links.append(new_path)
        else:
            await update_progress(
                progress_msg,
                f"❌ Failed: `{err}`",
                remove_keyboard=True,
                task_id=task_id,
                running_tasks=running_tasks,
            )

    return final_links