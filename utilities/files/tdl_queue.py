"""
tdl_queue.py
────────────
Single source of truth for all tdl process serialisation.

tdl uses a shared bolt DB (tdl.db) that cannot handle concurrent access.
This module exposes:
  - A single asyncio.Lock  (_tdl_lock)
  - A single waiters list  (_tdl_waiters)
  - queue_tdl_task()      — the only entry-point both upload and download use.
  - cancel_queued_user()  — called by cancel.py to cancel a WAITING user.
  - is_user_in_queue()    — called by cancel.py to check before cancelling.

Neither upload_download.py nor files.py should import asyncio.Lock or
maintain their own waiters list.  Everything goes through here.
"""

import asyncio
import time
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from main.config import logger
from vareon_analytics.vr_log import log_to_db

# ── One lock, one waiters list — shared by uploads AND downloads ──────────────
_tdl_lock: asyncio.Lock = asyncio.Lock()
_tdl_waiters: list[dict] = []
# Each entry:
#   {
#     "user_id":      int,
#     "kind":         "upload" | "download",
#     "file_name":    str,
#     "cancel_event": asyncio.Event   ← set this to wake & abort the waiter
#   }


def _make_cancel_btn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_process", style="danger")]
    ])


# ── Public helpers used by cancel.py ──────────────────────────────────────────

def is_user_in_queue(user_id: int) -> bool:
    """Returns True if this user is currently waiting in the tdl queue."""
    return any(e["user_id"] == user_id for e in _tdl_waiters)


def cancel_queued_user(user_id: int) -> bool:
    """
    Signals the waiting coroutine for user_id to abort cleanly.
    Returns True if the user was found and signalled, False otherwise.

    Works by setting the asyncio.Event in their queue entry.
    queue_tdl_task() races the lock-acquire against this event,
    so the user exits the queue immediately without ever starting tdl.
    """
    for entry in _tdl_waiters:
        if entry["user_id"] == user_id:
            entry["cancel_event"].set()
            logger.info(
                f"[QUEUE] Cancel signal sent to queued user {user_id} "
                f"({entry['kind']} of '{entry['file_name']}')"
            )
            return True
    return False


def queue_position(user_id: int) -> int:
    """1-based position of user_id in the global waiters list. 0 if not found."""
    for i, entry in enumerate(_tdl_waiters):
        if entry["user_id"] == user_id:
            return i + 1
    return 0


# ── Main queue entry-point ────────────────────────────────────────────────────

async def queue_tdl_task(
    *,
    progress_msg,
    context,
    user_id: int,
    file_name: str,
    kind: str,
    task_fn,
):
    """
    Universal queue entry-point for any tdl operation.

    Queues the caller, notifies them of their position, then waits for the
    global lock.  While waiting it also watches a per-user cancel_event so
    that cancel.py can abort the wait instantly without the user ever
    reaching the front of the queue.
    """
    active = context.user_data.get("active_process")
    if active and active.returncode is None:
        await progress_msg.edit_text(
            f"⚠️ *You already have a {kind} in progress!*\n\n"
            "Please wait for it to finish or press ❌ Cancel first.",
            parse_mode="Markdown",
        )
        return

    cancel_btn   = _make_cancel_btn()
    cancel_event = asyncio.Event()

    entry = {
        "user_id":      user_id,
        "kind":         kind,
        "file_name":    file_name,
        "cancel_event": cancel_event,
    }
    _tdl_waiters.append(entry)
    position = len(_tdl_waiters)
    queue_join_time = time.time()
    logger.info(
        f"[QUEUE] User {user_id} joined for {kind} of '{file_name}'. "
        f"Position: {position}  |  Queue depth: {len(_tdl_waiters)}"
    )

    # ── Log queue join event ───────────────────────────────────────────────────
    task_id = context.user_data.get("task_id")
    vareon_id = context.user_data.get("vareon_id_cache")  # may be None; best-effort
    try:
        from main.state import sessions
        session_data = sessions.get(user_id, {})
        vareon_id = session_data.get("vareon_id", vareon_id)
    except Exception:
        pass
    if task_id:
        log_to_db(
            vareon_id=vareon_id,
            tg_user_id=user_id,
            event_type="QUEUE_JOINED",
            function_name="queue_tdl_task",
            task_id=task_id,
            details={
                "kind": kind,
                "queue_position": position,
            },
            action_status={"status": "waiting"},
        )

    try:
        ahead = []
        if _tdl_lock.locked():
            active_entry = {
                "file_name": "Currently processing...",
                "kind": "running"
            }
            ahead.append(active_entry)
        ahead.extend(_tdl_waiters[:-1])
        position = len(ahead)
        if position == 0:
            logger.info(
                f"[QUEUE] User {user_id} has no wait — starting {kind} immediately "
                f"('{file_name}')"
            )
        else:
            try:
                await progress_msg.edit_text(
                    f"🕐 *Queued — Position {position}*\n"
                    f"`{file_name}`\n\n"
                    f"Your {kind} will start automatically when it's your turn.",
                    parse_mode="Markdown",
                    reply_markup=cancel_btn,
                )
                logger.info(f"[QUEUE] Showed queue position {position} to user {user_id}")
            except Exception as e:
                logger.warning(f"[QUEUE] Could not show queue msg to user {user_id} pos {position}: {e}")

        lock_task   = asyncio.ensure_future(_tdl_lock.acquire())
        cancel_task = asyncio.ensure_future(cancel_event.wait())

        done, pending = await asyncio.wait(
            [lock_task, cancel_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        if cancel_task in done:
            if lock_task in done:
                try:
                    if lock_task.result():
                        _tdl_lock.release()
                except Exception:
                    pass

            logger.info(f"[QUEUE] User {user_id} cancelled while waiting in queue ({kind}).")
            try:
                await progress_msg.edit_text(
                    f"❌ *{kind.capitalize()} cancelled.*\n`{file_name}`",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            return

        try:
            if entry in _tdl_waiters:
                _tdl_waiters.remove(entry)
                logger.info(
                    f"[QUEUE] User {user_id} dequeued — lock acquired, starting {kind}. "
                    f"Remaining waiters: {len(_tdl_waiters)}"
                )

            # ── Log queue wait complete ────────────────────────────────────────
            wait_seconds = round(time.time() - queue_join_time)
            if task_id:
                log_to_db(
                    vareon_id=vareon_id,
                    tg_user_id=user_id,
                    event_type="QUEUE_WAIT_COMPLETE",
                    function_name="queue_tdl_task",
                    task_id=task_id,
                    details={
                        "kind": kind,
                        "queue_wait_seconds": wait_seconds,
                    },
                    action_status={"status": "in_progress"},
                )

            if context.user_data.get("tdl_cancelled"):
                logger.info(f"[QUEUE] tdl_cancelled flag set for user {user_id} — aborting {kind}.")
                try:
                    await progress_msg.edit_text(
                        f"❌ *{kind.capitalize()} cancelled.*\n`{file_name}`",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
                return

            logger.info(
                f"[QUEUE] User {user_id} acquired lock — starting {kind} of '{file_name}'"
            )
            await task_fn(cancel_btn)

        finally:
            _tdl_lock.release()

    finally:
        if entry in _tdl_waiters:
            _tdl_waiters.remove(entry)
            logger.info(
                f"[QUEUE] User {user_id} removed from queue (finally-cleanup). "
                f"Remaining: {len(_tdl_waiters)}"
            )