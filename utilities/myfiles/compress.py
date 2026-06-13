from collections import defaultdict
import os, uuid, re
import shutil, subprocess, time
import asyncio
import time
import zipfile
user_locks = defaultdict(asyncio.Lock)
from pathlib import Path
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ForceReply
from telegram.ext import ContextTypes
from main.utils import ( 
    register_path,
    format_size,
    ensure_folder_permissions,  
    format_speed,
    format_time,
)
from utilities.myfiles.browse import refresh_folder_menu
from main.config import logger
_cancel_flags: dict[str, bool] = {}

########Compression#########
############################
async def compress(update, context):
    query = update.callback_query
    data = query.data.split("|")
    action = data[0]
    
    is_multi = action == "multi_compress"
    
    if not is_multi:
        uid = data[1]
        path = context.user_data.get("path_map", {}).get(uid)
        if not path or not os.path.exists(path):
            await query.edit_message_text("❌ File not found.")
            return
        # Clean up any stale single-compress state before storing new one
        context.user_data.pop("compress_uid", None)
        context.user_data.pop("compress_path", None)
        context.user_data["compress_uid"] = uid
        context.user_data["compress_path"] = path
        display_name = os.path.basename(path)
        callback_prefix = f"compress_format|{uid}"
    else:
        selected = context.user_data.get("selected_uids", set())
        if not selected:
            await query.answer("❌ No items selected.", show_alert=True)
            return
        display_name = f"{len(selected)} items"
        callback_prefix = "multi_exec"

    keyboard = [
        [
            InlineKeyboardButton("ZIP", callback_data=f"{callback_prefix}|zip"),
            InlineKeyboardButton("RAR", callback_data=f"{callback_prefix}|rar")
        ],
        [
            InlineKeyboardButton("7Z", callback_data=f"{callback_prefix}|7z"),
            InlineKeyboardButton("TAR.GZ", callback_data=f"{callback_prefix}|tar.gz")
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_compress", style="danger")
        ]
    ]
    
    await query.edit_message_text(
        text=f"📄 Target: {display_name}\n\nSelect compression format:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
async def compress_format(update, context, main_directory):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split("|")
    action = parts[0]

    path_map = context.user_data.get("path_map", {})
    file_paths = []

    try:
        if action == "multi_exec":
            format_type = parts[1]
            selected_uids = context.user_data.get("selected_uids", set())
            # Gather all valid paths from selected UIDs
            for uid in selected_uids:
                path = path_map.get(uid)
                if path and os.path.exists(path):
                    file_paths.append(path)
                    
        # ── SINGLE MODE ────────────────────────────
        elif parts[0] == "compress_format":
            _, uid, format_type = parts

            path = (
                path_map.get(uid)
                or context.user_data.get("compress_path")
            )

            # fallback rescan (same as your original)
            if not path:
                current_path = (context.user_data.get("path_stack") or [None])[-1]
                if current_path and os.path.isdir(current_path):
                    for entry in os.scandir(current_path):
                        entry_uid = register_path(context, entry.path)
                        if entry_uid == uid:
                            path = entry.path
                            break

            if not path or not os.path.exists(path):
                await query.edit_message_text("❌ File not found.")
                return

            file_paths = [path]

        else:
            await query.edit_message_text("❌ Invalid compression request.")
            return

    except Exception:
        await query.edit_message_text("❌ Invalid compression request.")
        return

    # ── VALIDATION ───────────────────────────────
    if not file_paths:
        await query.edit_message_text("❌ No valid files found.")
        return

    parent_dir = os.path.dirname(file_paths[0])

    try:
        ensure_folder_permissions(parent_dir)
    except Exception as e:
        logger.error(f"Permission error: {e}")
        await query.edit_message_text("❌ Permission error.")
        return

    context.user_data[f"pending_compress_{format_type}_{int(time.time())}"] = {
        "file_paths": file_paths,
        "format_type": format_type,
        "parent_dir": parent_dir
    }

    # ── ASK FOR NAME ─────────────────────────────
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"✍️ **Enter name for your {format_type.upper()} archive:**\n(Reply to this message with text only)",
        parse_mode="Markdown",
        reply_markup=ForceReply(selective=True)
    )
  
async def update_compression_progress(context, chat_id, message_id, compressed, total, start_time, filename, cancel_key=None, task_id=None):
    if total <= 0:
        return
    percent = min((compressed / total) * 100, 100)
    elapsed = max(time.time() - start_time, 0.1)
    speed = compressed / elapsed
    remaining = ((total - compressed) / speed) if speed > 0 else 0

    bar_filled = int(percent // 10)
    bar = "▪️" * bar_filled + "▫️" * (10 - bar_filled)

    text = (
        f"🔒 *Compressing* `{filename}`\n\n"
        f"*Progress:* {percent:.1f}%\n"
        f"{bar}\n"
        f"*{format_size(compressed)} of {format_size(total)}*\n"
        f"🚀 *Speed:* {format_speed(speed)}\n"
        f"⏳ *ETA:* {format_time(int(remaining))}s"
    )

    markup = None
    if cancel_key:
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_process|{task_id}", style="danger")]
        ])

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=markup
        )
    except Exception:
        pass

def _compress_paths(file_paths, output_path, format_type, is_cancelled):
    if is_cancelled():
        raise asyncio.CancelledError()

    # All items share the same parent dir — run CLI from there with bare names
    # so archives never embed the full absolute path (fixes vareon_id wrapping bug)
    work_dir = os.path.dirname(file_paths[0])
    rel_names = [os.path.basename(p) for p in file_paths]

    if format_type == "zip":
        CHUNK_SIZE = 1024 * 1024
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for path in file_paths:
                if os.path.isfile(path):
                    arcname = os.path.basename(path)
                    with open(path, 'rb') as f_in:
                        with zf.open(arcname, 'w', force_zip64=True) as f_out:
                            while True:
                                if is_cancelled(): raise Exception("cancelled")
                                chunk = f_in.read(CHUNK_SIZE)
                                if not chunk: break
                                f_out.write(chunk)
                elif os.path.isdir(path):
                    folder_name = os.path.basename(path)
                    for dirpath, _, filenames in os.walk(path):
                        for filename in filenames:
                            if is_cancelled(): raise Exception("cancelled")
                            full = os.path.join(dirpath, filename)
                            arcname = os.path.join(folder_name, os.path.relpath(full, path))
                            zf.write(full, arcname)

    elif format_type == "rar":
        rar_exe = shutil.which("rar")
        if not rar_exe:
            raise Exception("RAR not found")
        # -r recurses into folders; cwd ensures no absolute paths stored
        cmd = [rar_exe, "a", "-ep1", "-r", output_path] + rel_names
        process = subprocess.Popen(cmd, cwd=work_dir)
        while process.poll() is None:
            if is_cancelled():
                process.terminate()
                raise Exception("cancelled")
            time.sleep(0.5)
        if process.returncode != 0:
            raise Exception("RAR compression failed")

    elif format_type == "7z":
        exe = shutil.which("7z") or shutil.which("7za")
        if not exe: raise Exception("7z not found")
        # cwd ensures relative paths; -r recurses folders
        cmd = [exe, "a", "-y", "-r", "-bsp1", output_path] + rel_names
        process = subprocess.Popen(cmd, cwd=work_dir, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        while process.poll() is None:
            if is_cancelled():
                process.terminate()
                process.wait()
                if os.path.exists(output_path): os.remove(output_path)
                raise Exception("cancelled")
            line = process.stdout.readline()
            match = re.search(r'(\d+)%', line)
            if match:
                _cancel_flags[f"7z_progress_{output_path}"] = int(match.group(1))
        if process.returncode != 0: raise Exception("7z failed")

    elif format_type == "tar.gz":
        exe = shutil.which("tar")
        if not exe: raise Exception("tar not found")
        # -C sets working dir so only bare names are stored, not full paths
        cmd = [exe, "-czf", output_path, "-C", work_dir] + rel_names
        process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while process.poll() is None:
            if is_cancelled():
                process.terminate()
                process.wait()
                if os.path.exists(output_path): os.remove(output_path)
                raise Exception("cancelled")
            time.sleep(0.5)
        if process.returncode != 0: raise Exception("tar failed")

    else:
        raise Exception("Unsupported format")
    
async def compress_file(context, chat_id, file_path, output_path, format_type, custom_name):
    loop = asyncio.get_running_loop()

    file_paths = file_path if isinstance(file_path, list) else [file_path]
    display_name = f"{custom_name}.{format_type}"
        
    def _path_size(p):
        if os.path.isfile(p):
            return os.path.getsize(p)
        total = 0
        for dirpath, _, filenames in os.walk(p):
            for f in filenames:
                try: total += os.path.getsize(os.path.join(dirpath, f))
                except OSError: pass
        return total

    total_size = sum(_path_size(p) for p in file_paths)
    start_time = time.time()
    task_id = str(uuid.uuid4())
    cancel_key = f"compress_cancel_{task_id}"

    context.user_data[cancel_key] = False
    context.user_data[f"active_compression_{task_id}"] = output_path
    context.user_data["active_compress_task_id"] = task_id
    
    msg = await context.bot.send_message(
        chat_id,
        f"🔒 Compressing `{display_name}`...",
        parse_mode="Markdown",
    )

    def is_cancelled():
        return context.user_data.get(cancel_key, False)

    try:
        logger.info(f"Compressing {file_paths} → {output_path} as {format_type}")

        task = loop.run_in_executor(
            None,
            _compress_paths,
            file_paths,
            output_path,
            format_type,
            is_cancelled
        )

        last_update = start_time

        while not task.done():
            await asyncio.sleep(1)

            if is_cancelled():
                raise asyncio.CancelledError()

            if time.time() - last_update >= 2:
                if format_type == "7z":
                    pct = _cancel_flags.get(f"7z_progress_{output_path}", 0)
                    current_out = int((pct / 100) * total_size)
                else:
                    current_out = os.path.getsize(output_path) if os.path.exists(output_path) else 0


                await update_compression_progress(
                    context,
                    chat_id,
                    msg.message_id,
                    min(current_out, total_size),
                    total_size,
                    start_time,
                    display_name,
                    cancel_key=cancel_key,
                    task_id=task_id 
                )
                last_update = time.time()
        await task

        elapsed = time.time() - start_time
        logger.info(f"Compression completed: {output_path}")

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg.message_id,
            text=f"✅ `{display_name}` compressed in {elapsed:.1f}s!",
            parse_mode="Markdown"
        )

    except asyncio.CancelledError:
        logger.info(f"[CANCEL] Compression cancelled: {output_path}")
        try:
            await task  
        except Exception:
            pass

        if os.path.exists(output_path):
            try: os.remove(output_path)
            except Exception: pass

    except Exception as e:
        logger.error(f"Compression failed: {e}")
        if os.path.exists(output_path):
            try: os.remove(output_path)
            except Exception: pass

        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg.message_id,
            text=f"❌ Compression failed: {str(e)}",
            parse_mode="Markdown"
        )

    finally:
        context.user_data.pop(cancel_key, None)
        context.user_data.pop("active_compress_task_id", None)
        context.user_data.pop(f"active_compression_{task_id}", None) 
        _cancel_flags.pop(f"7z_progress_{output_path}", None)