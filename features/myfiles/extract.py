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
from main.utils import register_path
from main.config import logger

######### Extract ##########
############################
# ─────────────────────────────────────────────
# Folder Menu
# ─────────────────────────────────────────────

ITEMS_PER_PAGE = 30

async def show_extraction_folder_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    query = update.callback_query
    path_stack = context.user_data.get("move_path_stack", [])
    current_path = path_stack[-1] if path_stack else None

    if not current_path or not os.path.exists(current_path):
        await query.edit_message_text("❌ Directory not found.")
        return

    # Filter directories and sort
    items = [d for d in os.scandir(current_path) if d.is_dir()]
    items.sort(key=lambda e: e.name.lower())

    # Pagination logic
    total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1 if items else 1
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    current_items = items[start_idx:end_idx]

    keyboard = []

    # Folder buttons
    for entry in current_items:
        uid = register_path(context, entry.path)
        keyboard.append([InlineKeyboardButton(f"📁 {entry.name}", callback_data=f"extract_nav|{uid}")])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"extract_page|{page-1}"))
    if end_idx < len(items):
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"extract_page|{page+1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    # Action buttons
    keyboard.append([InlineKeyboardButton("📤 Extract Here", callback_data="extract_execute", style="primary")])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="extract_nav_back", style="primary")])

    msg = f"📂 Select a folder to extract into:\n"
    if total_pages > 1:
        msg += f"(Page {page + 1} of {total_pages})"

    await query.edit_message_text(
        msg,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ─────────────────────────────────────────────
# Progress Helper
# ─────────────────────────────────────────────

def _fmt_size(b):
    if b >= 1024 ** 3: return f"{b / 1024 ** 3:.2f} GB"
    if b >= 1024 ** 2: return f"{b / 1024 ** 2:.2f} MB"
    if b >= 1024:      return f"{b / 1024:.2f} KB"
    return f"{b:.0f} B"

def _bar(percent):
    filled = int(percent // 10)
    return "▪️" * filled + "▫️" * (10 - filled)

async def _update_progress(bot, chat_id, message_id, extracted, total, start_time, file_name, cancel_key=None, task_id=None):
    if total <= 0:
        return
    percent = min((extracted / total) * 100, 100)
    elapsed = max(time.time() - start_time, 0.1)
    speed = extracted / elapsed
    remaining = ((total - extracted) / speed) if speed > 0 else 0

    text = (
        f"📤 *Extracting* `{file_name}`\n\n"
        f"*Progress:* {percent:.1f}%\n"
        f"{_bar(percent)}\n"
        f"*{_fmt_size(extracted)} of {_fmt_size(total)}*\n"
        f"🚀 *Speed:* {_fmt_size(speed)}/s\n"
        f"⏳ *ETA:* {int(remaining)}s"
    )

    markup = None
    if cancel_key:
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_process|{task_id}", style="danger")]
        ])

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=markup
        )
    except Exception:
        pass

# ─────────────────────────────────────────────
# Multi-volume chain detection
# ─────────────────────────────────────────────

def _resolve_entry_point(file_path: str) -> str:
    """
    Given any part of a multi-volume archive, return the first part
    that the library should be opened with.

    Patterns handled:
      • RAR old-style  : file.rar + file.r00, file.r01 ...  → file.rar
      • RAR new-style  : file.part1.rar, file.part2.rar ... → file.part1.rar
      • 7z split       : file.7z.001, file.7z.002 ...       → file.7z.001
      • ZIP split      : file.zip, file.z01, file.z02 ...   → file.zip
      • Generic .001   : file.001, file.002 ...              → file.001
    """
    name = os.path.basename(file_path)
    directory = os.path.dirname(file_path)
    lower = name.lower()

    # RAR new-style: something.partN.rar  →  find part1
    m = re.match(r'^(.+?)\.part(\d+)\.rar$', lower)
    if m:
        base = name[:m.end(1)]          # preserve original case
        first = os.path.join(directory, f"{base}.part1.rar")
        return first if os.path.exists(first) else file_path

    # 7z split: something.7z.001 / .002 ...  →  .7z.001
    if re.search(r'\.7z\.\d+$', lower):
        base = re.sub(r'\.7z\.\d+$', '', name)
        first = os.path.join(directory, f"{base}.7z.001")
        return first if os.path.exists(first) else file_path

    # Generic numeric split: something.001 / .002 ...
    if re.search(r'\.\d{3}$', lower):
        base = re.sub(r'\.\d{3}$', '', name)
        first = os.path.join(directory, f"{base}.001")
        return first if os.path.exists(first) else file_path

    # RAR old-style: file.r00, file.r01 ...  →  file.rar
    if re.search(r'\.r\d{2}$', lower):
        base = re.sub(r'\.r\d{2}$', '', name)
        first = os.path.join(directory, f"{base}.rar")
        return first if os.path.exists(first) else file_path

    # Split ZIP: file.z01, file.z02 ...  →  file.zip
    if re.search(r'\.z\d{2}$', lower):
        base = re.sub(r'\.z\d{2}$', '', name)
        first = os.path.join(directory, f"{base}.zip")
        return first if os.path.exists(first) else file_path

    # Already the first part (or a normal single-volume archive)
    return file_path


def _group_volumes(file_paths: list[str]) -> list[str]:
    """
    Given a list of selected paths that may include multiple parts of the
    same multi-volume archive, collapse them down to one entry-point path
    per logical archive (deduplicating volumes of the same set).
    """
    seen: set[str] = set()
    result: list[str] = []
    for p in file_paths:
        entry = _resolve_entry_point(p)
        if entry not in seen:
            seen.add(entry)
            result.append(entry)
    return result

# ─────────────────────────────────────────────
# Sync core — runs in executor thread
# ─────────────────────────────────────────────
def _extract_paths(file_paths: list[str], extraction_dir: str, overwrite: bool, is_cancelled, progress: dict) -> None:
    for file_path in file_paths:
        if is_cancelled():
            raise Exception("cancelled")

        name_lower = os.path.basename(file_path).lower()
        suffixes = "".join(Path(file_path).suffixes).lower()
        file_size = os.path.getsize(file_path)

        # ── ZIP ───────────────────────────────────────
        if suffixes.endswith(".zip") or re.search(r'\.z\d{2}$', name_lower):
            entry = _resolve_entry_point(file_path)
            with zipfile.ZipFile(entry, 'r') as zf:
                members = zf.infolist()
                total_compressed = sum(m.compress_size for m in members)
                done_compressed = 0
                for member in members:
                    if is_cancelled(): raise Exception("cancelled")
                    target = os.path.join(extraction_dir, member.filename)
                    if not overwrite and os.path.exists(target):
                        done_compressed += member.compress_size
                        progress["bytes_read"] += member.compress_size
                        continue
                    zf.extract(member, extraction_dir)
                    done_compressed += member.compress_size
                    progress["bytes_read"] += member.compress_size

        # ── RAR ───────────────────────────────────────
        elif suffixes.endswith(".rar") or ".part" in name_lower:
            entry = _resolve_entry_point(file_path)
            exe = shutil.which("rar") or shutil.which("unrar")
            if not exe: raise Exception("RAR not found")
            cmd = [exe, "x", "-y", entry, extraction_dir + "/"]
            process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while process.poll() is None:
                if is_cancelled():
                    process.terminate()
                    process.wait()
                    raise Exception("cancelled")
                # Approximate: report source file size read proportionally over time
                progress["bytes_read"] = min(
                    int(file_size * min((time.time() - _extract_paths._start if hasattr(_extract_paths, '_start') else 1), 1)),
                    file_size
                )
                time.sleep(0.1)
            progress["bytes_read"] += file_size
            if process.returncode != 0: raise Exception("RAR extraction failed")

        # ── 7Z ────────────────────────────────────────
        elif suffixes.endswith(".7z") or re.search(r'\.7z\.\d+$', name_lower):
            entry = _resolve_entry_point(file_path)
            exe = shutil.which("7z") or shutil.which("7za") or shutil.which("7zz")
            if not exe: raise Exception("7z not found")
            cmd = [exe, "x", "-y", f"-o{extraction_dir}", entry]
            process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            proc_start = time.time()
            while process.poll() is None:
                if is_cancelled():
                    process.terminate()
                    process.wait()
                    raise Exception("cancelled")
                elapsed = time.time() - proc_start
                progress["bytes_read"] = min(int(file_size * (elapsed / max(elapsed, 1))), file_size)
                time.sleep(0.5)
            progress["bytes_read"] += file_size
            if process.returncode != 0: raise Exception("7z extraction failed")

        # ── TAR ────────────────────────────────────────
        elif any(suffixes.endswith(s) for s in (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")):
            exe = shutil.which("tar")
            if not exe: raise Exception("tar not found")
            cmd = [exe, "-xf", file_path, "-C", extraction_dir]
            process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            proc_start = time.time()
            while process.poll() is None:
                if is_cancelled():
                    process.terminate()
                    process.wait()
                    raise Exception("cancelled")
                # Can't know exact progress from tar without --checkpoint, so poll source file size over time
                elapsed = time.time() - proc_start
                progress["bytes_read"] = min(int(file_size * (elapsed / max(elapsed, 1))), file_size)
                time.sleep(0.5)
            progress["bytes_read"] += file_size
            if process.returncode != 0: raise Exception("TAR extraction failed")

        # ── Generic .001 ──────────────────────────────
        elif re.search(r'\.\d{3}$', name_lower):
            sevenzip_exe = shutil.which("7z") or shutil.which("7za")
            if not sevenzip_exe:
                raise Exception(f"Unsupported split format and `7z` CLI not found: {file_path}")
            entry = _resolve_entry_point(file_path)
            cmd = [sevenzip_exe, "x", "-y", f"-o{extraction_dir}", entry]
            proc = subprocess.Popen(cmd)
            proc_start = time.time()
            while proc.poll() is None:
                if is_cancelled():
                    proc.terminate()
                    raise Exception("cancelled")
                elapsed = time.time() - proc_start
                progress["bytes_read"] = min(int(file_size * (elapsed / max(elapsed, 1))), file_size)
                time.sleep(0.5)
            progress["bytes_read"] += file_size
            if proc.returncode != 0:
                raise Exception(f"7z CLI failed extracting {entry}")

        else:
            raise Exception(f"Unsupported format: {suffixes or name_lower}")
# ─────────────────────────────────────────────
# Async orchestrator — single AND multi
# ─────────────────────────────────────────────
async def extract_archive(
    context, chat_id, file_paths, extraction_dir,
    overwrite=False, display_name=None, extract_mode="flat",
    existing_msg_id=None, extra_cancel_key=None,
    task_id=None
):
    loop = asyncio.get_running_loop()

    if isinstance(file_paths, str):
        file_paths = [file_paths]

    resolved = _group_volumes(file_paths)

    if not display_name:
        display_name = os.path.basename(resolved[0]) if len(resolved) == 1 else f"{len(resolved)} archives"

    if not task_id:
        task_id = str(uuid.uuid4())
    cancel_key = f"extract_cancel_{task_id}"
    if cancel_key not in context.user_data:
        context.user_data[cancel_key] = False
    context.user_data["active_extract_task_id"] = task_id
    os.makedirs(extraction_dir, exist_ok=True)

    total_size = sum(os.path.getsize(p) for p in resolved if os.path.exists(p))
    start_time = time.time()

    # Shared mutable counter so the thread can report progress
    progress = {"bytes_read": 0}

    if existing_msg_id:
        msg_id = existing_msg_id
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=f"📤 Extracting `{display_name}`...",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_process|{task_id}", style="danger")
                ]])
            )
        except Exception:
            pass
    else:
        sent_msg = await context.bot.send_message(
            chat_id,
            f"📤 Extracting `{display_name}`...",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_process|{task_id}", style="danger")
            ]])
        )
        msg_id = sent_msg.message_id

    def is_cancelled():
        return (
            context.user_data.get(cancel_key, False)
            or bool(extra_cancel_key and context.user_data.get(extra_cancel_key, False))
        )

    try:
        task = loop.run_in_executor(None, _extract_paths, resolved, extraction_dir, overwrite, is_cancelled, progress)
        last_update = 0

        while not task.done():
            await asyncio.sleep(0.5)

            if is_cancelled():
                raise Exception("cancelled")

            now = time.time()
            if now - last_update >= 2.5:
                await _update_progress(
                    context.bot, chat_id, msg_id,
                    progress["bytes_read"], total_size,   # ← use bytes_read, not output size
                    start_time, display_name, cancel_key=cancel_key,
                    task_id=task_id
                )
                last_update = now

        await task
        elapsed = time.time() - start_time
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text=f"✅ `{display_name}` extracted in {elapsed:.1f}s!",
            parse_mode="Markdown"
        )
        return True

    except Exception as e:
        if "cancelled" in str(e).lower():
            logger.info(f"[CANCEL] Extraction cancelled: {extraction_dir}")
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text="⏹️ Extraction cancelled."
            )
        else:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=f"❌ Extraction failed: `{str(e)}`"
            )
        return False
    finally:
        context.user_data.pop(cancel_key, None)
        context.user_data.pop("active_extract_task_id", None)
# ─────────────────────────────────────────────
# Multi-extract entry point
# ─────────────────────────────────────────────

async def multi_extract(update, context):
    """
    Triggered when the user taps Extract in multi-select mode.

    • Collects all selected paths and deduplicates multi-volume chains
      (e.g. selecting both file.part1.rar and file.part2.rar is fine).
    • Each logical archive is extracted into its own subfolder inside
      the parent directory (always-to-folder behaviour).
    • All extractions are fired as independent async tasks so they run
      concurrently and each shows its own progress message.
    """
    query = update.callback_query
    await query.answer()

    selected_uids = context.user_data.get("selected_uids", set())
    if not selected_uids:
        await query.edit_message_text("❌ No items selected.")
        return

    # Check if we have valid files
    path_map = context.user_data.get("path_map", {})
    raw_paths = [path_map.get(uid) for uid in selected_uids if path_map.get(uid) and os.path.isfile(path_map.get(uid))]

    if not raw_paths:
        await query.edit_message_text("❌ No valid archive files found.")
        return

    context.user_data["multi_extract_paths"] = raw_paths
    multi_cancel_id = str(uuid.uuid4())
    context.user_data["multi_extract_cancel_id"] = multi_cancel_id
    context.user_data[f"extract_cancel_{multi_cancel_id}"] = False

    keyboard = [
        [InlineKeyboardButton("📂 Extract Here Separately", callback_data="extract_multi_sep")],
        [InlineKeyboardButton("📁 Extract to Single Folder", callback_data="extract_multi_single_prompt")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_process|{multi_cancel_id}", style="danger")]
    ]

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"📦 *Multi-Extraction*\nSelected: `{len(raw_paths)}` archives\n\nChoose extraction method:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def handle_extract_multi_sep(update, context):
    query = update.callback_query
    await query.answer()

    raw_paths = context.user_data.get("multi_extract_paths", [])
    logical_archives = _group_volumes(raw_paths)

    multi_cancel_id = context.user_data.get("multi_extract_cancel_id")
    multi_cancel_key = f"extract_cancel_{multi_cancel_id}" if multi_cancel_id else None

    msg = await query.edit_message_text(
        f"📤 Starting separate extraction of {len(logical_archives)} archive(s)...",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_process|{multi_cancel_id}", style="danger")
        ]]) if multi_cancel_id else None
    )
    # Fire and forget — return immediately so the dispatcher stays free
    asyncio.create_task(
        _run_multi_sep_loop(context, update.effective_chat.id, logical_archives, msg.message_id, multi_cancel_id, multi_cancel_key)
    )
    
async def _run_multi_sep_loop(context, chat_id, logical_archives, msg_id, multi_cancel_id, multi_cancel_key):
    total = len(logical_archives)
    overall_start = time.time()
    done = 0

    def is_multi_cancelled():
        return multi_cancel_key and context.user_data.get(multi_cancel_key, False)

    for index, entry_path in enumerate(logical_archives, 1):
        if is_multi_cancelled():
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=f"⏹️ Multi-extraction cancelled after {done}/{total} archives."
            )
            break

        archive_stem = re.sub(
            r'\.(part\d+\.rar|7z\.\d+|\d{3}|tar\.(gz|bz2|xz)|zip|rar|7z|tar|tgz)$',
            '', os.path.basename(entry_path), flags=re.IGNORECASE
        )
        extraction_dir = os.path.join(os.path.dirname(entry_path), archive_stem)
        if os.path.exists(extraction_dir) and os.listdir(extraction_dir):
            extraction_dir += "_extracted"
        os.makedirs(extraction_dir, exist_ok=True)
        register_path(context, extraction_dir)

        file_start = time.time()
        result = await extract_archive(
            context, chat_id, entry_path, extraction_dir,
            overwrite=False, display_name=os.path.basename(entry_path),
            existing_msg_id=msg_id,
            extra_cancel_key=multi_cancel_key,
            task_id=multi_cancel_id       # ← also apply the previous fix here
        )
        done += 1
        if not result:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=f"⏹️ Multi-extraction cancelled after {done}/{total} archives."
            )
            break

        file_elapsed = time.time() - file_start
        overall_elapsed = time.time() - overall_start
        summary = "\n".join([
            f"📦 *Extraction Progress* — `{done}/{total}` done",
            f"",
            f"✅ `{os.path.basename(entry_path)}` in {file_elapsed:.1f}s",
            f"⏱ Total time so far: {overall_elapsed:.1f}s",
            f"{'🏁 All done!' if done == total else f'⏳ Next: `{os.path.basename(logical_archives[index])}`'}",
        ])
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=summary, parse_mode="Markdown"
            )
        except Exception:
            pass

    context.user_data.pop("multi_extract_paths", None)
    context.user_data.pop("multi_extract_cancel_id", None)
    if multi_cancel_key:
        context.user_data.pop(multi_cancel_key, None)
        
async def prompt_single_folder_name(update, context):
    """Deletes choice menu and asks for folder name via ForceReply"""
    query = update.callback_query
    await query.answer()
 
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="✍️ **Enter the name for the new extraction folder:**\n(Reply to this message with the name only)",
        parse_mode="Markdown",
        reply_markup=ForceReply(selective=True)
    )
    
async def _run_multi_single_loop(context, chat_id, logical_archives, msg_id, folder_name, target_dir, multi_cancel_id, multi_cancel_key):
    total = len(logical_archives)
    overall_start = time.time()
    done = 0

    def is_multi_cancelled():
        return multi_cancel_key and context.user_data.get(multi_cancel_key, False)

    for index, entry_path in enumerate(logical_archives, 1):
        if is_multi_cancelled():
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=f"⏹️ Multi-extraction cancelled after {done}/{total} archives."
            )
            break

        file_start = time.time()
        result = await extract_archive(
            context, chat_id, entry_path, target_dir,
            overwrite=False, display_name=os.path.basename(entry_path),
            existing_msg_id=msg_id,
            extra_cancel_key=multi_cancel_key,
            task_id=multi_cancel_id
        )
        done += 1
        if not result:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=f"⏹️ Multi-extraction cancelled after {done}/{total} archives."
            )
            break

        file_elapsed = time.time() - file_start
        overall_elapsed = time.time() - overall_start

        summary = "\n".join([
            f"📁 *Extracting into* `{folder_name}` — `{done}/{total}` done",
            f"",
            f"✅ `{os.path.basename(entry_path)}` in {file_elapsed:.1f}s",
            f"⏱ Total time so far: {overall_elapsed:.1f}s",
            f"{'🏁 All done!' if done == total else f'⏳ Next: `{os.path.basename(logical_archives[index])}`'}",
        ])

        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=summary, parse_mode="Markdown"
            )
        except Exception:
            pass

    context.user_data.pop("multi_extract_paths", None)
    context.user_data.pop("multi_extract_cancel_id", None)
    if multi_cancel_key:
        context.user_data.pop(multi_cancel_key, None)