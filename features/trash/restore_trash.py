import os
import shutil
from telegram import Update
from telegram.ext import ContextTypes
from features.trash.helper import _get_db, _clear_select_state, _resolve_dest_path
from main.config import logger, USERS_PATH

async def _do_restore_all(update: Update, query, context: ContextTypes.DEFAULT_TYPE, vareon_id: int):
    """
    Restores all files in trash for this user:
      1. Fetch all rows for vareon_id from DB
      2. Move each from .trash back to original_path (with conflict rename)
      3. Delete each DB row on success
    """
    conn, cursor = _get_db()
    restored = 0
    failed   = []

    try:
        cursor.execute(
            "SELECT id, filename, trash_filename, original_path FROM trash_files WHERE vareon_id = ?",
            (vareon_id,)
        )
        rows = cursor.fetchall()

        for row in rows:
            trash_path = f"{USERS_PATH}/{vareon_id}/.trash/{row['trash_filename']}"
            dest_path  = _resolve_dest_path(row["original_path"])

            try:
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                shutil.move(trash_path, dest_path)
                cursor.execute("DELETE FROM trash_files WHERE id = ?", (row["id"],))
                restored += 1
            except Exception as e:
                logger.error(f"Restore all failed for {row['filename']}: {e}")
                failed.append(row["filename"])

        conn.commit()
    finally:
        conn.close()

    if failed:
        logger.warning(f"Restore all — failed for: {failed}")
    else:
        logger.info(f"Restore all — successfully restored {restored} files for vareon_id={vareon_id}")
        
async def _do_restore_selected(update: Update, query, context: ContextTypes.DEFAULT_TYPE, selected_ids: set):
    """
    For each selected file:
      1. Build current trash path from vareon_id + trash_filename
      2. Resolve conflict-safe destination (original_path)
      3. Move file back, renaming if needed
      4. Delete the DB row
    Then exit select mode and re-render trash.
    """
    conn, cursor = _get_db()
    restored  = 0
    failed    = []

    try:
        for file_id in selected_ids:
            cursor.execute(
                "SELECT vareon_id, filename, trash_filename, original_path FROM trash_files WHERE id = ?",
                (file_id,)
            )
            row = cursor.fetchone()
            if not row:
                continue  # already gone

            trash_path = f"{USERS_PATH}/{row['vareon_id']}/.trash/{row['trash_filename']}"
            dest_path  = _resolve_dest_path(row["original_path"])

            try:
                # Make sure the destination folder exists
                # (edge case: user deleted the parent folder while file was in trash)
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                shutil.move(trash_path, dest_path)
                cursor.execute("DELETE FROM trash_files WHERE id = ?", (file_id,))
                restored += 1
            except Exception as e:
                logger.error(f"Restore failed for {row['filename']}: {e}")
                failed.append(row["filename"])

        conn.commit()
        if restored > 0:
            logger.info(f"Successfully restored {restored} files from trash.")
    
    finally:
        conn.close()

    # Exit select mode
    _clear_select_state(context)

    # Show result summary as a toast, then re-render
    if failed:
        logger.warning(f"Failed to restore {len(failed)} files from trash: {failed}")
       
async def _do_restore_single(update: Update, query, context: ContextTypes.DEFAULT_TYPE, file_id: str):
    """
    Restores a single file/folder from trash back to its original location.
    """
    conn, cursor = _get_db()
    try:
        cursor.execute(
            "SELECT vareon_id, filename, trash_filename, original_path FROM trash_files WHERE id = ?",
            (file_id,)
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    if not row:
        await query.answer("⚠️ File not found in trash.", show_alert=True)
        return

    trash_path = f"{USERS_PATH}/{row['vareon_id']}/.trash/{row['trash_filename']}"
    dest_path  = _resolve_dest_path(row["original_path"])

    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        shutil.move(trash_path, dest_path)
        conn, cursor = _get_db()
        cursor.execute("DELETE FROM trash_files WHERE id = ?", (file_id,))
        conn.commit()
        conn.close()
        logger.info(f"Restored single file: {row['filename']} → {dest_path}")
    except Exception as e:
        logger.error(f"Single restore failed for {row['filename']}: {e}")
        await query.answer("⚠️ Restore failed. Try again.", show_alert=True)