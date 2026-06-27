import os
import shutil
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from features.trash.helper import _get_db
from main.config import logger, USERS_PATH


# ─── Purge Job ────────────────────────────────────────────────────────────────

def run_trash_purge():
    """
    Scans trash_files table for entries older than 30 days.
    For each expired file:
      1. Delete from disk (.trash folder)
      2. Delete the DB row
    Runs every night at 12:00 AM.
    """
    logger.info("[PURGE] Starting scheduled trash purge...")

    cutoff = datetime.now() - timedelta(days=30)
    conn, cursor = _get_db()
    deleted = 0
    failed  = []

    try:
        cursor.execute(
            "SELECT id, vareon_id, filename, trash_filename FROM trash_files WHERE deleted_at <= ?",
            (cutoff.isoformat(),)
        )
        expired_rows = cursor.fetchall()

        if not expired_rows:
            logger.info("[PURGE] No expired files found. Nothing to purge.")
            return

        logger.info(f"[PURGE] Found {len(expired_rows)} expired file(s). Purging...")

        for row in expired_rows:
            trash_path = f"{USERS_PATH}/{row['vareon_id']}/.trash/{row['trash_filename']}"

            try:
                if os.path.exists(trash_path):
                    if os.path.isdir(trash_path):
                        shutil.rmtree(trash_path)
                    else:
                        os.remove(trash_path)

                cursor.execute("DELETE FROM trash_files WHERE id = ?", (row["id"],))
                deleted += 1
                logger.info(f"[PURGE] Deleted: {row['filename']} (vareon_id={row['vareon_id']})")

            except Exception as e:
                logger.error(f"[PURGE] Failed to delete {row['filename']}: {e}")
                failed.append(row["filename"])

        conn.commit()

    except Exception as e:
        logger.error(f"[PURGE] Unexpected error during purge: {e}")
    finally:
        conn.close()

    logger.info(f"[PURGE] Done. Deleted: {deleted}, Failed: {len(failed)}")
    if failed:
        logger.warning(f"[PURGE] Failed files: {failed}")


# ─── Scheduler Setup ──────────────────────────────────────────────────────────

def start_purge_scheduler():
    """
    Sets up and starts the APScheduler with a SQLite job store.
    The job store remembers the last scheduled run time so if the bot
    restarts and midnight was missed, it runs the purge immediately on startup.

    Call this once in bot.py before application.run_polling().
    """
    jobstores = {
        "default": SQLAlchemyJobStore(url="sqlite:////var/lib/vareon/apscheduler.db")
    }

    job_defaults = {
        "misfire_grace_time": 3600  # if missed by less than 1 hour, run immediately on restart
    }

    scheduler = AsyncIOScheduler(jobstores=jobstores, job_defaults=job_defaults)

    # Runs every night at 12:00 AM
    scheduler.add_job(
        run_trash_purge,
        trigger="cron",
        hour=0,
        minute=0,
        id="trash_purge",          # fixed ID so jobstore doesn't create duplicates on restart
        replace_existing=True
    )

    scheduler.start()
    logger.info("[PURGE] Trash purge scheduler started. Runs daily at 12:00 AM.")