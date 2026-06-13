from main.config import logger
from telegram.ext import CallbackContext
from main.state import (awaiting_cookie, awaiting_id, report_buffer, report_mode, report_priority, report_state, 
                        report_subject)

STATE_MAP = {
    "cookies": [awaiting_cookie],
    "getid": [awaiting_id],
    "report": [
        report_mode,
        report_buffer,
        report_state,
        report_subject,
        report_priority,
    ],
}

async def cancel_active_state(user_id: int, context: CallbackContext):
    """
    Cancels any active 'waiting' states like cookie input, ID input, etc.
    Returns the name of the cancelled state (or None).
    
    DO NOT EDIT THIS
    """

    for state_name, state_dicts in STATE_MAP.items():

        found = False

        for state in state_dicts:
            if user_id in state:
                state.pop(user_id, None)
                found = True

        if found:
            logger.info(f"[CANCEL] Cleared state {state_name} for {user_id}")
            return state_name

    return None

def clear_conv_handlers(context: CallbackContext) -> bool: 
    """
    Cancel Conversation Handlers from Here
    """
    cleared = False

    # 🔹 Rename
    if "rename_uid" in context.user_data:
        context.user_data.pop("rename_uid", None)
        context.user_data.pop("is_folder", None)
        cleared = True

    # 🔹 New Folder
    if "new_folder_path" in context.user_data:
        context.user_data.pop("new_folder_path", None)
        cleared = True

    return cleared