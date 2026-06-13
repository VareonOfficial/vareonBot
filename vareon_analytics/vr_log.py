import sqlite3
import json, asyncio
import time
from datetime import datetime, timezone
from functools import wraps
from main.config import VAREON_DB, logger
from main.state import sessions
import inspect
import uuid

def generate_task_id() -> str:
    return str(uuid.uuid4())[:12]
# =============================================
# HELPER: Calculate monthly stats from live_logs
# =============================================
def _calculate_monthly_stats(cursor, vareon_id, tg_user_id, year, month):
    month_str = f"{month:02d}"
    year_str = str(year)

    # Active days
    cursor.execute("""
        SELECT COUNT(DISTINCT strftime('%Y-%m-%d', timestamp))
        FROM live_logs
        WHERE vareon_id = ? AND tg_user_id = ?
        AND strftime('%Y', timestamp) = ?
        AND strftime('%m', timestamp) = ?
    """, (vareon_id, tg_user_id, year_str, month_str))
    active_days = cursor.fetchone()[0] or 0

    # Most active hour
    cursor.execute("""
        SELECT strftime('%H', timestamp) as h, COUNT(*) as cnt
        FROM live_logs
        WHERE vareon_id = ? AND tg_user_id = ?
        AND strftime('%Y', timestamp) = ?
        AND strftime('%m', timestamp) = ?
        GROUP BY h ORDER BY cnt DESC LIMIT 1
    """, (vareon_id, tg_user_id, year_str, month_str))
    hour_row = cursor.fetchone()
    most_active_hour = int(hour_row[0]) if hour_row else None

    # Most active weekday
    cursor.execute("""
        SELECT strftime('%w', timestamp) as wd, COUNT(*) as cnt
        FROM live_logs
        WHERE vareon_id = ? AND tg_user_id = ?
        AND strftime('%Y', timestamp) = ?
        AND strftime('%m', timestamp) = ?
        GROUP BY wd ORDER BY cnt DESC LIMIT 1
    """, (vareon_id, tg_user_id, year_str, month_str))
    wday_row = cursor.fetchone()
    most_active_weekday = int(wday_row[0]) if wday_row else None

    return active_days, most_active_hour, most_active_weekday


# =============================================
# HELPER: Upsert monthly_stats
# =============================================
def _update_monthly_stats(cursor, vareon_id, tg_user_id, year, month,
                           timestamp_str, event_type, function_name,
                           is_success, is_failed):

    # Create row if not exists
    cursor.execute("""
        INSERT OR IGNORE INTO monthly_stats 
        (vareon_id, tg_user_id, year, month, first_activity, last_activity)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (vareon_id, tg_user_id, year, month, timestamp_str, timestamp_str))

    # Fetch current JSON counters
    cursor.execute("""
        SELECT event_type_counts, function_name_counts
        FROM monthly_stats
        WHERE vareon_id = ? AND tg_user_id = ? AND year = ? AND month = ?
    """, (vareon_id, tg_user_id, year, month))

    row = cursor.fetchone()
    event_counts = json.loads(row[0] or '{}')
    func_counts = json.loads(row[1] or '{}')

    # Increment JSON counters
    event_counts[event_type] = event_counts.get(event_type, 0) + 1
    func_counts[function_name] = func_counts.get(function_name, 0) + 1

    # Calculate stats fresh from live_logs
    active_days, most_active_hour, most_active_weekday = _calculate_monthly_stats(
        cursor, vareon_id, tg_user_id, year, month
    )

    # Update the row
    cursor.execute("""
        UPDATE monthly_stats SET
            total_actions        = total_actions + 1,
            success_count        = success_count + ?,
            failed_count         = failed_count + ?,
            event_type_counts    = ?,
            function_name_counts = ?,
            active_days          = ?,
            most_active_hour     = ?,
            most_active_weekday  = ?,
            last_activity        = ?
        WHERE vareon_id = ? AND tg_user_id = ? AND year = ? AND month = ?
    """, (
        is_success, is_failed,
        json.dumps(event_counts),
        json.dumps(func_counts),
        active_days,
        most_active_hour,
        most_active_weekday,
        timestamp_str,
        vareon_id, tg_user_id, year, month
    ))


# =============================================
# CORE LOGGING FUNCTION
# =============================================
def log_to_db(
    vareon_id,
    tg_user_id: int,
    event_type: str,
    function_name: str,
    task_id: str = None,
    details: dict = None,
    action_status: dict = None
) -> None:
    if details is None:
        details = {}
    if action_status is None:
        action_status = {}

    details_json = json.dumps(details)
    action_status_json = json.dumps(action_status)
    timestamp = datetime.now(timezone.utc)
    timestamp_str = timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")

    year = timestamp.year
    month = timestamp.month

    status = action_status.get("status", "unknown")
    is_success = 1 if status == "success" else 0
    is_failed = 1 if status in ("failed", "error") else 0

    conn = sqlite3.connect(VAREON_DB)
    cursor = conn.cursor()

    try:
        # ── 1. Insert into live_logs ──────────────────────────
        cursor.execute("""
            INSERT INTO live_logs 
            (vareon_id, tg_user_id, event_type, function_name, task_id, details, action_status, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            vareon_id, tg_user_id, event_type, function_name,
            task_id, details_json, action_status_json, timestamp_str
        ))

        # ── 2. Update monthly_stats ───────────────────────────
        _update_monthly_stats(
            cursor, vareon_id, tg_user_id, year, month,
            timestamp_str, event_type, function_name,
            is_success, is_failed
        )

        conn.commit()

    except Exception as e:
        logger.error(f"[live_logs] ERROR while logging: {e}")
    finally:
        conn.close()
        
# =============================================
# 2. FIXED ASYNC-AWARE LOG WRAPPER
# =============================================
def log_wrapper(
    event_type: str = "COMMAND",
    function_name: str = None,
    auto_extract_user: bool = True
):
    def decorator(func):
        fn_name = function_name or func.__name__

        @wraps(func)
        async def wrapper(*args, **kwargs):
            start_time = time.time()

            tg_user_id = None
            vareon_id = None

            # Auto extract from Update object
            if auto_extract_user and args:
                first_arg = args[0]
                if hasattr(first_arg, "effective_user") and first_arg.effective_user:
                    tg_user_id = first_arg.effective_user.id

            # Allow manual override via kwargs
            tg_user_id = kwargs.get("tg_user_id") or tg_user_id
            vareon_id = kwargs.get("vareon_id") or vareon_id

            # Get vareon_id from sessions
            if vareon_id is None and tg_user_id:
                try:
                    session_data = sessions.get(tg_user_id, {})
                    vareon_id = session_data.get("vareon_id")
                except:
                    pass

            # Final fallbacks
            if tg_user_id is None:
                tg_user_id = 0
            if vareon_id is None:
                vareon_id = "unknown"

            # ===================== EXECUTE FUNCTION =====================
            try:
                if inspect.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)
                status = "success"
            except Exception as e:
                status = "error"
                logger.error(f"Error in {fn_name}: {e}")
                raise
            finally:
                latency_ms = int((time.time() - start_time) * 1000)

                action_status = {
                    "action": fn_name,
                    "status": status,
                    "latency": f"{latency_ms}ms"
                }

                details = kwargs.get("details", {})

                log_to_db(
                    vareon_id=vareon_id,
                    tg_user_id=tg_user_id,
                    event_type=event_type,
                    function_name=fn_name,
                    details=details,
                    action_status=action_status
                )

            return result

        return wrapper

    return decorator