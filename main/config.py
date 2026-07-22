import os
import re
import pytz
import logging
import psycopg2
from pathlib import Path
from dotenv import load_dotenv
from logging.handlers import TimedRotatingFileHandler

env_file_path = os.getenv("ENV_FILE")
if env_file_path:
    load_dotenv(dotenv_path=env_file_path, override=True)

############################################
# BASE PATHS
############################################

BASE_DIR = Path.cwd()

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DATABASE_DIR = BASE_DIR / "databases" / "data"
DATABASE_DIR.mkdir(parents=True, exist_ok=True)

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

############################################
# ENV VARIABLES
############################################

# Telegram
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPPORT_BOT_TOKEN = os.getenv("SUPPORT_BOT_TOKEN")

# Admin
ADMIN_ID = int(os.getenv("ADMIN_ID"))

# Paths
BASE_PATH = Path(os.getenv("BASE_PATH", "/var/lib/vareon"))
USERS_PATH = BASE_PATH / "users"
COOKIES_PATH = BASE_PATH / "vareonusercontent" / "cookies"

# Login
LOGIN_LINK = os.getenv("LOGIN_LINK")

# Groups
SOS_GROUP_ID = int(os.getenv("SOS_GROUP_ID"))
SUPPORT_GROUP_ID = int(os.getenv("SUPPORT_GROUP_ID"))
PRIVATE_GROUP_ID = int(os.getenv("PRIVATE_GROUP_ID"))

PRIVATE_GROUP_LINK = os.getenv("PRIVATE_GROUP_LINK")

# Browser
CDP_URL = os.getenv("CDP_URL")
# Link Generate API
LINK_API_PORT = int(os.getenv("LINK_SERVICE_PORT", 5000))

YT_COOKIE_VIDEO_ID = os.getenv("YT_COOKIE_VIDEO_ID")
SPOTIFY_COOKIE_VIDEO_ID = os.getenv("SPOTIFY_COOKIE_VIDEO_ID")

############################################
# DATABASES
############################################

VAREON_DB = DATABASE_DIR / "vareon.db"

# TDL Database
STORAGE_PATH = BASE_DIR / "tdl_session" / "tdl.db"
TELETHON_SESSION_TXT = DATA_DIR / "telethon_user_session.txt"

############################################
# REGEX / TIMEZONE
############################################

URL_PATTERN = re.compile(
    r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
)

IST = pytz.timezone("Asia/Kolkata")

############################################
# TELEGRAM LIMITS
############################################

TELEGRAM_MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024

############################################
# RATE LIMITS
############################################

RATE_LIMIT_PER_MINUTE = 20
RATE_LIMIT_INTERVAL = 1.5

############################################
# PAGINATION
############################################

ITEMS_PER_PAGE = 10

############################################
# STATES
############################################

DOWNLOAD_LINK, MOVE_FOLDER = range(4, 6)

RENAME = 0
NEW_FOLDER = 14

############################################
# LOGGER
############################################

class CustomTimedRotatingFileHandler(
    TimedRotatingFileHandler
):
    def rotation_filename(self, default_name):
        """
        Convert:

        logs/latest.txt.2026-05-07

        Into:

        logs/2026-05-07.txt
        """

        date_part = default_name.split(".")[-1]

        return str(LOG_DIR / f"{date_part}.log")


log_formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(filename)s:%(lineno)d | %(message)s"
)

latest_log_file = LOG_DIR / "latest.log"

# Rewrite latest.txt fresh on every startup
latest_log_file.write_text("", encoding="utf-8")

file_handler = CustomTimedRotatingFileHandler(
    filename=latest_log_file,
    when="midnight",
    interval=1,
    backupCount=30,
    encoding="utf-8"
)

file_handler.setFormatter(log_formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

if not logger.handlers:
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

############################################
# POSTGRESQL
############################################

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD"),
    "port": int(os.getenv("DB_PORT", 5432)),
}
def pg_conn_auth():
    return psycopg2.connect(
        database="vareon",
        **DB_CONFIG
    )
def get_user_details_from_db(vareon_id: int):
    with pg_conn_auth() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, email, language, country,
                       vareon_username, zip, location
                FROM users
                WHERE vareon_id = %s
                """,
                (vareon_id,)
            )

            result = cur.fetchone()

            if not result:
                return None

            return {
                "name": result[0],
                "email": result[1],
                "language": result[2],
                "country": result[3],
                "vareon_username": result[4],
                "zip": result[5],
                "location": result[6],
            }