import time
from pyrogram import Client as PyroClient
from telethon import TelegramClient as TelethonClient
# Authentication & session state
sessions = {}          # user_id -> username
login_states = {}      # user_id -> login step
user_client = {}       # user_id -> TelegramClient instance

# Active operations
file_sessions = {}     # user_id -> file-related state
upload_tasks = {}      # user_id -> asyncio task
cancel_tokens = {}     # user_id -> cancel flag

# Runtime state
bot_start_time = time.time()

# Rate limiting & activity
user_last_interaction = {}   # user_id -> last timestamp
broadcast_mode = {}
notify_mode = {}          # user_id -> bool
broadcast_last_message_id = {}

# Report mode
report_mode = {}
report_buffer = {}
report_state = {}
report_subject = {}
report_priority = {}

# Defaults
download_status = {}
download_tasks = {}
cancel_upload = {}

# Uploading file
pending_uploads = {}

# Myfiles
user_selection_state = {} 

# Music 
awaiting_cookie = {}
running_tasks = {}

# File ID and User ID
awaiting_id = {}

app = None
PRIVATE_GROUP_ID = None
application = None
pyro_bot_client:      PyroClient     = None
telethon_user_client: TelethonClient = None
user_client = {}