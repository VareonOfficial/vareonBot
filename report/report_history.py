import sqlite3
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext
from main.config import VAREON_DB, logger, ITEMS_PER_PAGE

# Single source of truth for status icons and display names
STATUS_DATA = {
    "PENDING":  {"emoji": "🟡", "label": "Pending"},
    "UR":       {"emoji": "🔵", "label": "Under Review"},
    "RESOLVED": {"emoji": "✅", "label": "Fixed / Resolved"},
    "CLOSED":   {"emoji": "❌", "label": "Closed / Invalid"}
}

def get_db_connection():
    return sqlite3.connect(VAREON_DB, check_same_thread=False)

def parse_callback(data: str):
    """
    Parses callback data into (page, active_filter).
      report_history              → page=0, filter=ALL
      report_history:2            → page=2, filter=ALL
      report_history:2:PENDING    → page=2, filter=PENDING
    """
    parts = data.split(":")
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    active_filter = parts[2] if len(parts) > 2 else "ALL"
    return page, active_filter

def build_filter_row(active_filter: str) -> list:
    row = []
    # Iterate through the new consolidated dictionary
    for status, data in STATUS_DATA.items():
        is_active = (active_filter == status)
        button_style = "primary" if is_active else "default"
        next_filter = "ALL" if is_active else status
        
        row.append(InlineKeyboardButton(
            text=data['emoji'], 
            callback_data=f"report_history:0:{next_filter}",
            style=button_style
        ))
    return row

async def report_history(update: Update, context: CallbackContext):
    """
    Shows a paginated, filterable list of user reports with status emojis.
    Callback pattern: report_history:{page}:{filter}
    """
    query = update.callback_query
    user_id = update.effective_user.id
    await query.answer()

    page, active_filter = parse_callback(query.data or "")

    offset = page * ITEMS_PER_PAGE

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 1. Count total (respecting filter)
        if active_filter == "ALL":
            cursor.execute(
                "SELECT COUNT(*) FROM user_reports WHERE telegram_id = ?",
                (user_id,)
            )
        else:
            cursor.execute(
                "SELECT COUNT(*) FROM user_reports WHERE telegram_id = ? AND status = ?",
                (user_id, active_filter)
            )
        total_reports = cursor.fetchone()[0]

        # 2. Fetch page of reports (respecting filter)
        if active_filter == "ALL":
            cursor.execute("""
                SELECT report_uid, status, created_at
                FROM user_reports
                WHERE telegram_id = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (user_id, ITEMS_PER_PAGE, offset))
        else:
            cursor.execute("""
                SELECT report_uid, status, created_at
                FROM user_reports
                WHERE telegram_id = ? AND status = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (user_id, active_filter, ITEMS_PER_PAGE, offset))
        reports = cursor.fetchall()

        keyboard = []

        if not reports:
            if active_filter == "ALL":
                text = "📜 **Report History**\n\nYou haven't submitted any reports yet."
            else:
                emoji = STATUS_DATA[active_filter]["emoji"]
                label = STATUS_DATA[active_filter]["label"]
                text = f"📜 **Report History**\n\nNo {emoji} {label} reports found."

            keyboard.append(build_filter_row(active_filter))
            keyboard.append([InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="report_reopen_menu", style="primary")])

        else:
            if active_filter != "ALL":
                emoji = STATUS_DATA[active_filter]["emoji"]
                label = STATUS_DATA[active_filter]["label"]
                filter_note = f" — {emoji} {label} only"
            else:
                filter_note = ""
            legend = "\n".join([f"{v['emoji']} {v['label']}" for v in STATUS_DATA.values()])
            text = (
                f"📜 **Your Report History** (Page {page + 1}{filter_note})\n\n"
                f"{legend}\n\n"
                f"Select a report to view details:"
            )
            
            # 3. Report buttons
            for report_uid, status, created_at in reports:
                emoji = STATUS_DATA.get(status, {}).get("emoji", "⚪")
                keyboard.append([InlineKeyboardButton(
                    f"{emoji} {report_uid}",
                    callback_data=f"view_rep:{report_uid}:{page}:{active_filter}"
                )])

            # 4. Prev / Next pagination row
            nav_row = []
            if page > 0:
                nav_row.append(InlineKeyboardButton(
                    "⬅️ Prev",
                    callback_data=f"report_history:{page - 1}:{active_filter}"
                ))
            if (offset + ITEMS_PER_PAGE) < total_reports:
                nav_row.append(InlineKeyboardButton(
                    "Next ➡️",
                    callback_data=f"report_history:{page + 1}:{active_filter}"
                ))
            if nav_row:
                keyboard.append(nav_row)

            # 5. Filter toggle row
            keyboard.append(build_filter_row(active_filter))

            # 6. Back button
            keyboard.append([InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="report_reopen_menu", style="primary")])

        await query.edit_message_text(
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Error in report_history: {e}")
    finally:
        conn.close()


async def view_report_details(update: Update, context: CallbackContext):
    """
    Shows full details of a specific report including priority and last update.
    Callback pattern: view_rep:{uid}:{return_page}:{return_filter}
    """
    query = update.callback_query
    parts = query.data.split(":")
    report_uid = parts[1]
    return_page = parts[2] if len(parts) > 2 else "0"
    return_filter = parts[3] if len(parts) > 3 else "ALL"

    await query.answer()

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT vareon_id, status, created_at, priority, updated_at
        FROM user_reports
        WHERE report_uid = ?
    """, (report_uid,))
    report = cursor.fetchone()
    conn.close()

    if not report:
        await query.answer("⚠️ Report not found.", show_alert=True)
        return

    v_id, status, created, priority, updated = report
    emoji = STATUS_DATA.get(status, {}).get("emoji", "⚪")
    label = STATUS_DATA.get(status, {}).get("label", status)
    last_update = f"`{updated}`" if updated else "_No updates yet_"

    detail_text = (
        f"📑 **Report Details: {report_uid}**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🆔 **Vareon ID:** `{v_id}`\n"
        f"🚦 **Status:** {emoji} {label}\n"
        f"⚡ **Priority:** `{priority or 'N/A'}`\n"
        f"📅 **Submitted:** `{created}` UTC\n"
        f"🔄 **Last Update:** {last_update} UTC\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Contact support if you need to update this report."
    )

    keyboard = []
    # 1. Add Cancel button first
    if status == "PENDING":
        keyboard.append([InlineKeyboardButton(
            "🗑️ Cancel", 
            callback_data=f"rep_delete:{report_uid}:{return_page}:{return_filter}",
            style="danger"
        )])
    # 2. Add Back button second
    keyboard.append([InlineKeyboardButton(
        "⬅️ Back to List",
        callback_data=f"report_history:{return_page}:{return_filter}",
        style="primary"
    )])
    # 3. Add Main Menu button last
    keyboard.append([InlineKeyboardButton(
        "🏠 Main Menu", 
        callback_data="report_reopen_menu", 
        style="success"
    )])
    
    await query.edit_message_text(
        text=detail_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    
async def delete_report_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    
    parts = query.data.split(":")
    report_uid = parts[1]
    return_page = int(parts[2])
    return_filter = parts[3]

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM user_reports 
        WHERE report_uid = ? 
        AND telegram_id = ? 
        AND status = 'PENDING'
    """, (report_uid, user_id))
    
    deleted_rows = cursor.rowcount
    conn.commit()
    conn.close()

    if deleted_rows == 0:
        await query.answer("⚠️ Cannot delete. This report is already being processed.", show_alert=True)
        return

    await query.answer("🗑️ Report deleted.", show_alert=False)
    await query.edit_message_text(
        text="✅ Report deleted successfully.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to List", callback_data=f"report_history:{return_page}:{return_filter}")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="report_reopen_menu")]
        ])
    )