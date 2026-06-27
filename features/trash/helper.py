import sqlite3, os
from datetime import datetime, timedelta
from telegram.ext import ContextTypes
from main.config import VAREON_DB

# ─── Helpers ──────────────────────────────────────────────────────────────────
def _get_db():
    conn = sqlite3.connect(VAREON_DB)
    conn.row_factory = sqlite3.Row
    return conn, conn.cursor()


def _format_size(size_bytes: int) -> str:
    """Convert raw bytes to human-readable string."""
    if size_bytes is None:
        return "Unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def _file_icon(filename: str) -> str:
    """Return an emoji icon based on file extension."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in {"mp4", "mkv", "avi", "mov", "webm"}:
        return "🎬"
    if ext in {"mp3", "flac", "wav", "ogg", "m4a", "aac"}:
        return "🎵"
    if ext in {"jpg", "jpeg", "png", "gif", "webp", "bmp"}:
        return "🖼️"
    if ext in {"pdf", "doc", "docx", "txt", "epub"}:
        return "📄"
    if ext in {"zip", "rar", "7z", "tar", "gz"}:
        return "🗜️"
    return "📁"


def _auto_delete_days(deleted_at_iso: str, retention_days: int = 30) -> int:
    deleted_date = datetime.fromisoformat(deleted_at_iso).date()
    expire_date  = deleted_date + timedelta(days=retention_days)
    remaining    = (expire_date - datetime.now().date()).days
    return max(remaining, 0)


def _now_time() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _get_select_state(context: ContextTypes.DEFAULT_TYPE) -> tuple[bool, set]:
    """
    Returns (select_mode: bool, selected_ids: set).
    Safe to call even if keys don't exist yet.
    """
    select_mode  = context.user_data.get("trash_select_mode", False)
    selected_ids = context.user_data.get("trash_selected", set())
    return select_mode, selected_ids


def _get_page(context: ContextTypes.DEFAULT_TYPE) -> int:
    return context.user_data.get("trash_page", 0)


def _set_page(context: ContextTypes.DEFAULT_TYPE, page: int):
    context.user_data["trash_page"] = page


def _clear_select_state(context: ContextTypes.DEFAULT_TYPE):
    context.user_data["trash_select_mode"] = False
    context.user_data["trash_selected"]    = set()
    # keep the page as-is so user lands back on same page after cancelling
    
# ─── Bulk action implementations ─────────────────────────────────────────────

def _resolve_dest_path(original_path: str) -> str:
    """
    Returns a safe destination path.
    If original_path already exists on disk, appends (1), (2), ... before the extension
    until a free slot is found.
    e.g. /users/24/video.mp4 → /users/24/video (1).mp4
    """
    if not os.path.exists(original_path):
        return original_path

    # Split into base + extension so we insert the counter before the dot
    base, ext = os.path.splitext(original_path)
    counter = 1
    while True:
        candidate = f"{base} ({counter}){ext}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1
