import sqlite3
import json
import os
from datetime import datetime, timezone
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import mm

# ── Import your project config ──────────────────────────────────
from main.config import VAREON_DB, logger

def _format_ts(ts: str) -> str:
    if not ts or ts == "—":
        return "—"
    try:
        dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d %b %Y, %H:%M UTC")
    except Exception:
        return ts
# =============================================
# CORE: Fetch logs from DB
# =============================================
def _fetch_user_logs(vareon_id: str) -> list:
    conn = sqlite3.connect(VAREON_DB)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT event_type, details, action_status, timestamp, tg_user_id
            FROM live_logs
            WHERE vareon_id = ?
            ORDER BY timestamp ASC
        """, (vareon_id,))
        rows = cursor.fetchall()
        return rows
    except Exception as e:
        logger.error(f"[export_data] DB fetch error: {e}")
        return []
    finally:
        conn.close()

def _fetch_summary(vareon_id: str) -> dict:
    conn = sqlite3.connect(VAREON_DB)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT COUNT(*), MIN(timestamp), MAX(timestamp), GROUP_CONCAT(DISTINCT tg_user_id)
            FROM live_logs
            WHERE vareon_id = ?
        """, (vareon_id,))
        row = cursor.fetchone()
        return {
            "total_events": row[0] or 0,
            "first_activity": _format_ts(row[1]),
            "last_activity": _format_ts(row[2]),
            "linked_tg_ids": row[3] or "—",
        }
    except Exception as e:
        logger.error(f"[export_data] Summary fetch error: {e}")
        return {}
    finally:
        conn.close()
# =============================================
# CORE: Build PDF
# =============================================
def generate_export_pdf(vareon_id: str, tg_user_id: int, output_path: str) -> bool:
    logs = _fetch_user_logs(vareon_id)
    summary = _fetch_summary(vareon_id)

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
    )

    styles = getSampleStyleSheet()

    # ── Custom styles ────────────────────────────────────────────
    title_style = ParagraphStyle(
        "VareonTitle",
        parent=styles["Title"],
        fontSize=20,
        textColor=colors.HexColor("#1a1a2e"),
        spaceAfter=4,
    )
    subtitle_style = ParagraphStyle(
        "VareonSubtitle",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#666666"),
        spaceAfter=2,
    )
    section_heading_style = ParagraphStyle(
        "SectionHeading",
        parent=styles["Heading2"],
        fontSize=11,
        textColor=colors.HexColor("#1a1a2e"),
        spaceBefore=10,
        spaceAfter=4,
    )
    label_style = ParagraphStyle(
        "Label",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#444444"),
    )
    event_type_style = ParagraphStyle(
        "EventType",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#0077b6"),
        fontName="Helvetica-Bold",
    )
    detail_key_style = ParagraphStyle(
        "DetailKey",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#555555"),
        fontName="Helvetica-Bold",
    )
    detail_val_style = ParagraphStyle(
        "DetailVal",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#222222"),
    )

    story = []

    # ── Header ───────────────────────────────────────────────────
    story.append(Paragraph("Vareon — Personal Data Export", title_style))
    story.append(Paragraph(
        f"Generated on {datetime.now(timezone.utc).strftime('%d %B %Y, %H:%M UTC')}",
        subtitle_style
    ))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 6))

    # ── Account Info ─────────────────────────────────────────────
    story.append(Paragraph("Account Information", section_heading_style))

    account_data = [
        ["Vareon ID", str(vareon_id)],
        ["Linked Telegram IDs", summary.get("linked_tg_ids", "—")],
        ["Total Events Logged", str(summary.get("total_events", 0))],
        ["First Activity", summary.get("first_activity", "—")],
        ["Last Activity", summary.get("last_activity", "—")],
    ]

    account_table = Table(account_data, colWidths=[50 * mm, 120 * mm])
    account_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#444444")),
        ("TEXTCOLOR", (1, 0), (1, -1), colors.HexColor("#111111")),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.HexColor("#f5f5f5"), colors.white]),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#dddddd")),
    ]))
    story.append(account_table)
    story.append(Spacer(1, 10))

    # ── Activity Log ─────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#cccccc")))
    story.append(Paragraph("Activity Log", section_heading_style))

    if not logs:
        story.append(Paragraph("No activity found for this account.", label_style))
    else:
        for event_type, details_raw, status_raw, timestamp, log_tg_id in logs:
            # ── Timestamp + Event type row ───────────────────────
            try:
                dt = datetime.strptime(timestamp[:19], "%Y-%m-%d %H:%M:%S")
                ts_display = dt.strftime("%d %b %Y  %H:%M UTC")
            except Exception:
                ts_display = timestamp

            try:
                status_dict = json.loads(status_raw or "{}")
                status_str = status_dict.get("status", "—")
            except Exception:
                status_str = "—"

            try:
                details_dict = json.loads(details_raw or "{}")
            except Exception:
                details_dict = {}

            # Header row for this event
            header_data = [[
                Paragraph(ts_display, label_style),
                Paragraph(event_type, event_type_style),
                Paragraph(f"Status: {status_str}", label_style),
                Paragraph(f"TG: {log_tg_id}", label_style),
            ]]
            header_table = Table(header_data, colWidths=[45*mm, 60*mm, 40*mm, 25*mm])
            header_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eaf4fb")),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cce0f0")),
            ]))
            story.append(header_table)

            # Details rows
            if details_dict:
                detail_rows = []
                for k, v in details_dict.items():
                    # Truncate very long values
                    val_str = str(v)
                    if len(val_str) > 200:
                        val_str = val_str[:200] + "..."
                    detail_rows.append([
                        Paragraph(k, detail_key_style),
                        Paragraph(val_str, detail_val_style),
                    ])

                detail_table = Table(detail_rows, colWidths=[50 * mm, 120 * mm])
                detail_table.setStyle(TableStyle([
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.HexColor("#fafafa"), colors.white]),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("LEFTPADDING", (0, 0), (0, -1), 14),  # indent keys
                    ("LEFTPADDING", (1, 0), (1, -1), 8),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#eeeeee")),
                ]))
                story.append(detail_table)

            story.append(Spacer(1, 4))

    # ── Footer note ──────────────────────────────────────────────
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc")))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "This document was generated by Vareon and contains your personal activity data. "
        "Internal system fields such as task IDs and function names are excluded.",
        ParagraphStyle("Footer", parent=styles["Normal"], fontSize=7, textColor=colors.HexColor("#999999"))
    ))

    try:
        doc.build(story)
        logger.info(f"[export_data] PDF generated: {output_path}")
        return True
    except Exception as e:
        logger.error(f"[export_data] PDF build error: {e}")
        return False


# =============================================
# TELEGRAM HANDLER: /export_data command
# =============================================
async def handle_export_data(update, context):
    tg_user_id = update.effective_user.id
    from main.state import sessions

    session = sessions.get(tg_user_id, {})
    vareon_id = session.get("vareon_id")

    if not vareon_id:
        await update.message.reply_text(
            "⚠️ You are not logged in to a Vareon account.\n"
            "Please log in first and try again."
        )
        return

    msg = await update.message.reply_text("⏳ Generating your data export, please wait...")

    # Output path — temp file
    output_path = f"/tmp/vareon_export_{vareon_id}_{tg_user_id}.pdf"


    success = generate_export_pdf(vareon_id, tg_user_id, output_path)

    if not success or not os.path.exists(output_path):
        await msg.edit_text("❌ Failed to generate your data export. Please try again later.")
        return

    await msg.edit_text("📄 Sending your data export...")

    try:
        with open(output_path, "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                filename=f"vareon_data_export_{vareon_id}.pdf",
                caption=(
                    "📋 <b>Your Vareon Data Export</b>\n\n"
                    "This PDF contains all your activity logs stored on Vareon.\n"
                    "You can request a fresh export anytime using /export_data."
                ),
                parse_mode="HTML",
            )
        await msg.delete()
    except Exception as e:
        logger.error(f"[export_data] Send error: {e}")
        await msg.edit_text("❌ Failed to send the file. Please try again.")
    finally:
        # Clean up temp file
        try:
            os.remove(output_path)
        except Exception:
            pass