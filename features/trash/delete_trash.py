import os
import shutil
from telegram import Update
from telegram.ext import ContextTypes
from features.trash.helper import _get_db, _clear_select_state
from main.config import logger, USERS_PATH

async def _do_delete_selected(update: Update, query, context: ContextTypes.DEFAULT_TYPE, selected_ids: set):
    """
    For each selected file:
      1. Build current trash path from vareon_id + trash_filename
      2. Delete the file from disk
      3. Delete the DB row
    Then exit select mode and re-render trash.
    """
    conn, cursor = _get_db()
    deleted = 0
    failed  = []

    try:
        for file_id in selected_ids:
            cursor.execute(
                "SELECT vareon_id, filename, trash_filename FROM trash_files WHERE id = ?",
                (file_id,)
            )
            row = cursor.fetchone()
            if not row:
                continue  # already gone

            trash_path = f"{USERS_PATH}/{row['vareon_id']}/.trash/{row['trash_filename']}"

            try:
                if os.path.exists(trash_path):
                    if os.path.isdir(trash_path):
                        shutil.rmtree(trash_path)
                    else:
                        os.remove(trash_path)
                cursor.execute("DELETE FROM trash_files WHERE id = ?", (file_id,))
                deleted += 1
            except Exception as e:
                logger.error(f"Delete failed for {row['filename']}: {e}")
                failed.append(row["filename"])

        conn.commit()
        if deleted > 0:
            logger.info(f"Successfully permanently deleted {deleted} files from trash.")
            
    except Exception as e:
        logger.error(f"Delete failed for file_id={file_id}: {e}")
        failed.append(row["filename"])
    finally:
        conn.close()

    # Exit select mode
    _clear_select_state(context)

    if failed:
        logger.warning(f"Failed to delete {len(failed)} files from trash: {failed}")
        

async def _do_empty_bin(update: Update, query, context: ContextTypes.DEFAULT_TYPE, vareon_id: int):
    """
    Deletes the entire .trash folder for the user (removes all files including
    any orphaned ones not in DB), then recreates it empty, then clears all DB rows.
    """
    trash_folder = f"{USERS_PATH}/{vareon_id}/.trash"
    conn, cursor = _get_db()

    try:
        try:
            if os.path.exists(trash_folder):
                shutil.rmtree(trash_folder)
            os.makedirs(trash_folder, exist_ok=True)
            cursor.execute("DELETE FROM trash_files WHERE vareon_id = ?", (vareon_id,))
            conn.commit()
            logger.info(f"Emptied trash for vareon_id={vareon_id}")
        except Exception as e:
            logger.error(f"Empty bin failed for vareon_id={vareon_id}: {e}")
    finally:
        conn.close()
        
async def _do_delete_single(update: Update, query, context: ContextTypes.DEFAULT_TYPE, file_id: str):
    """
    Permanently deletes a single file/folder from trash and clears its DB row.
    """
    conn, cursor = _get_db()
    try:
        cursor.execute(
            "SELECT vareon_id, filename, trash_filename FROM trash_files WHERE id = ?",
            (file_id,)
        )
        row = cursor.fetchone()
    finally:
        conn.close()

    if not row:
        await query.answer("⚠️ File not found in trash.", show_alert=True)
        return

    trash_path = f"{USERS_PATH}/{row['vareon_id']}/.trash/{row['trash_filename']}"

    try:
        if os.path.exists(trash_path):
            if os.path.isdir(trash_path):
                shutil.rmtree(trash_path)
            else:
                os.remove(trash_path)
        conn, cursor = _get_db()
        cursor.execute("DELETE FROM trash_files WHERE id = ?", (file_id,))
        conn.commit()
        conn.close()
        logger.info(f"Permanently deleted single file: {row['filename']}")
    except Exception as e:
        logger.error(f"Single delete failed for {row['filename']}: {e}")
        await query.answer("⚠️ Delete failed. Try again.", show_alert=True)
