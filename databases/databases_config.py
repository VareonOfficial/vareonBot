import sqlite3
from main.config import VAREON_DB, logger


# ==============================
# 🔹 DB Version Helpers
# ==============================
def get_db_version(cursor):
    cursor.execute("SELECT value FROM db_meta WHERE key = 'db_version'")
    row = cursor.fetchone()
    return int(row[0]) if row else 0


def set_db_version(cursor, version):
    cursor.execute("""
        INSERT INTO db_meta (key, value)
        VALUES ('db_version', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (str(version),))


# ==============================
# 🔹 Migrations
# ==============================
def run_migrations(cursor):
    current_version = get_db_version(cursor)

    # ------------------------------
    # ✅ Version 1 → Base Tables
    # ------------------------------
    if current_version < 5:
        #cursor.execute("DROP TABLE IF EXISTS live_logs")
        cursor.execute("DROP TABLE IF EXISTS broadcast_settings")
        cursor.execute("DELETE FROM download_links;")
        cursor.execute("DROP TABLE IF EXISTS user_reports")
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS restore_users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                vareon_id INTEGER
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS telegram_auth (
                vareon_id INTEGER,
                telegram_user_id INTEGER,
                telegram_username TEXT,
                telegram_full_name TEXT,
                latest_login_at TIMESTAMP,
                first_login_at TIMESTAMP,
                PRIMARY KEY (vareon_id, telegram_user_id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                broadcast_id TEXT,
                telegram_user_id INTEGER,
                message_id INTEGER,
                timestamp TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                telegram_user_id INTEGER PRIMARY KEY,
                default_download_enabled INTEGER DEFAULT 0,
                default_download_path TEXT,
                receive_updates INTEGER DEFAULT 0
            )
        """)  
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_uid TEXT UNIQUE,
                telegram_id INTEGER,
                vareon_id INTEGER,
                username TEXT,
                full_name TEXT,
                total_messages INTEGER,
                message_summary TEXT,
                status TEXT DEFAULT 'PENDING',        -- PENDING, UR, RESOLVED, CLOSED
                group_msg_id INTEGER,
                priority TEXT CHECK(priority IN ('Low', 'Medium', 'High')),
                updated_at TIMESTAMP NULL,            -- Updates whenever status changes
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS support_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_number TEXT UNIQUE,
                telegram_user_id INTEGER,
                vareon_id TEXT,
                username TEXT,
                full_name TEXT,
                status TEXT DEFAULT 'PENDING',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ticket_msg_id INTEGER
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS download_links (
                token TEXT PRIMARY KEY,
                vareon_id TEXT,
                telegram_user_id INTEGER,
                file_path TEXT NOT NULL,
                filename TEXT NOT NULL,
                link_sharing TEXT DEFAULT 'off',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_support_user_id ON support_tickets (telegram_user_id);")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS live_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vareon_id TEXT NOT NULL,
                tg_user_id INTEGER,
                function_name TEXT,
                event_type TEXT,
                task_id TEXT DEFAULT NULL,
                details TEXT, -- Stores JSON string
                action_status TEXT, -- Stores JSON string: status, reason, latency
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Index to speed up monthly summary and vareon_id lookups
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vareon_live ON live_logs(vareon_id)")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS monthly_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vareon_id TEXT NOT NULL,
                tg_user_id INTEGER NOT NULL,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,  -- 1-12

                total_actions INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,

                event_type_counts TEXT DEFAULT '{}',    -- JSON: {"FILE_RECEIVED": 5, "DOWNLOAD_COMPLETE": 3}
                function_name_counts TEXT DEFAULT '{}', -- JSON: {"handle_file": 5, "start_youtube_download": 3}

                active_days INTEGER DEFAULT 0,          -- distinct calendar days used
                most_active_hour INTEGER DEFAULT NULL,  -- 0-23
                most_active_weekday INTEGER DEFAULT NULL, -- 0=Monday, 6=Sunday

                first_activity DATETIME DEFAULT NULL,
                last_activity DATETIME DEFAULT NULL,

                UNIQUE(vareon_id, tg_user_id, year, month)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS yearly_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vareon_id TEXT NOT NULL,
                tg_user_id INTEGER NOT NULL,
                year INTEGER NOT NULL,

                total_actions INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,

                event_type_counts TEXT DEFAULT '{}',    -- JSON: rolled up from monthly
                function_name_counts TEXT DEFAULT '{}', -- JSON: rolled up from monthly

                total_active_days INTEGER DEFAULT 0,    -- distinct days across full year
                active_months INTEGER DEFAULT 0,        -- how many months out of 12 were active
                most_active_month INTEGER DEFAULT NULL, -- 1-12
                most_active_hour INTEGER DEFAULT NULL,  -- 0-23 across full year
                most_active_weekday INTEGER DEFAULT NULL,

                first_activity DATETIME DEFAULT NULL,
                last_activity DATETIME DEFAULT NULL,

                UNIQUE(vareon_id, tg_user_id, year)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pending_uploads (
                vareon_id TEXT NOT NULL,
                telegram_id TEXT NOT NULL,
                uuid TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trash_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vareon_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                trash_filename TEXT NOT NULL,
                original_path TEXT NOT NULL,
                deleted_at TIMESTAMP NOT NULL,
                size INTEGER NOT NULL
            )
        """)
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS broadcast_settings (
                telegram_user_id INTEGER PRIMARY KEY,
                receive_updates INTEGER DEFAULT 1
            );

            INSERT OR IGNORE INTO broadcast_settings (telegram_user_id, receive_updates)
            SELECT telegram_user_id, 1
            FROM telegram_auth;
        """)
        set_db_version(cursor, 1)
# ==============================
# 🔹 Initialize DB
# ==============================
def init_db():
    """
    Initialize and migrate database schema.
    Safe to call multiple times.
    """
    try:
        conn = sqlite3.connect(VAREON_DB)
        cursor = conn.cursor()

        # Meta table (required for versioning)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS db_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Run migrations
        run_migrations(cursor)

        conn.commit()
        conn.close()

        logger.info("Database initialized & migrated successfully.")

    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        