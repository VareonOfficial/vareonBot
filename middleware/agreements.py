"""
middleware.py
──────────────
Generic "agreement gate" engine — currently used for the policy
acceptance flow, built to support future agreement types
(e.g. notification opt-ins, feature betas) without new tables.
"""

import sqlite3, os
from collections import defaultdict
from datetime import datetime, timezone
from main.config import VAREON_DB, logger

# ── agreement_type -> {telegram_user_id: version} ─────────────────────────
_tg_cache: dict[str, dict[int, str]] = defaultdict(dict)
# ── agreement_type -> {vareon_id: version} ─────────────────────────────────
_vareon_cache: dict[str, dict[int, str]] = defaultdict(dict)

def init_agreements_table():
    conn = sqlite3.connect(VAREON_DB)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_agreements (
            telegram_user_id INTEGER NOT NULL,
            vareon_id         INTEGER,
            agreement_type    TEXT NOT NULL,
            version           TEXT NOT NULL,
            accepted_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (telegram_user_id, agreement_type)
        )
    """)
    conn.commit()
    conn.close()


def load_agreements_cache():
    """Call once at startup — populates both caches from DB."""
    conn = sqlite3.connect(VAREON_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_user_id, vareon_id, agreement_type, version FROM user_agreements")
    rows = cursor.fetchall()
    conn.close()

    for telegram_user_id, vareon_id, agreement_type, version in rows:
        _tg_cache[agreement_type][telegram_user_id] = version
        if vareon_id:
            _vareon_cache[agreement_type][vareon_id] = version

    logger.info(f"[AGREEMENTS] Loaded {len(rows)} agreement rows into cache")


def _record_agreement(telegram_user_id: int, vareon_id: int | None, agreement_type: str, version: str):
    conn = sqlite3.connect(VAREON_DB)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO user_agreements (telegram_user_id, vareon_id, agreement_type, version, accepted_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(telegram_user_id, agreement_type) DO UPDATE SET
            vareon_id=excluded.vareon_id,
            version=excluded.version,
            accepted_at=excluded.accepted_at
    """, (telegram_user_id, vareon_id, agreement_type, version, datetime.now(timezone.utc)))
    conn.commit()
    conn.close()

    _tg_cache[agreement_type][telegram_user_id] = version
    if vareon_id:
        _vareon_cache[agreement_type][vareon_id] = version


def _has_accepted(telegram_user_id: int, vareon_id: int | None, agreement_type: str, current_version: str) -> bool:
    # 1. direct check — this exact telegram account already accepted
    if _tg_cache[agreement_type].get(telegram_user_id) == current_version:
        return True

    # 2. cross-account check — same vareon_id accepted from a different telegram account
    if vareon_id and _vareon_cache[agreement_type].get(vareon_id) == current_version:
        # backfill so this telegram_id is recorded too — avoids repeating this check every update
        _record_agreement(telegram_user_id, vareon_id, agreement_type, current_version)
        return True

    return False

