import os
import sqlite3
import secrets
from flask import Flask, request, jsonify, abort, send_file
from main.config import VAREON_DB, logger

app = Flask(__name__)

# --- DATABASE UTILITIES ---

def get_db_connection():
    conn = sqlite3.connect(VAREON_DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes the table if it doesn't exist."""
    with get_db_connection() as conn:
        conn.execute("""
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
        conn.commit()

# --- API ROUTES ---

@app.route("/internal/create-link", methods=["POST"])
def internal_create_link():
    try:
        data = request.get_json(force=True)
        
        token = secrets.token_urlsafe(48)
        file_path = data.get("file_path")
        filename = data.get("filename")
        telegram_user_id = data.get("telegram_user_id")
        vareon_id = data.get("vareon_id")
        
        # Link sharing is 'off' by default as requested
        link_sharing = data.get("link_sharing", "off")

        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO download_links (token, vareon_id, telegram_user_id, file_path, filename, link_sharing)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (token, vareon_id, telegram_user_id, file_path, filename, link_sharing)
            )
            conn.commit()

        return jsonify({"token": token}), 200
    except Exception as e:
        logger.error(f"CREATE LINK ERROR: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/internal/revoke-link", methods=["POST"])
def revoke_link():
    data = request.get_json(force=True)
    token = data.get("token")

    if not token:
        return jsonify({"error": "missing token"}), 400

    try:
        with get_db_connection() as conn:
            cur = conn.execute("DELETE FROM download_links WHERE token = ?", (token,))
            conn.commit()
            affected = cur.rowcount

        return jsonify({"revoked": affected > 0}), 200
    except Exception as e:
        logger.error(f"REVOKE ERROR: {e}")
        return jsonify({"error": "internal error"}), 500

@app.route("/d/<token>")
def download(token):
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT file_path, filename, link_sharing FROM download_links WHERE token = ?", 
                (token,)
            ).fetchone()

        if not row:
            abort(404) 
        file_path = row["file_path"]
        if not os.path.isfile(file_path):
            abort(404)

        return send_file(
            file_path,
            as_attachment=True,
            download_name=row["filename"]
        )

    except Exception as e:
        logger.error(f"DOWNLOAD ERROR: {e}")
        abort(500)

# --- MAIN EXECUTION ---

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)