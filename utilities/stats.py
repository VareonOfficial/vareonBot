# utilities/stats.py

import asyncio
import subprocess
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from main.config import logger
from telegram import Update
from telegram.ext import ContextTypes
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler

# Import your global bot start time (adjust import path if needed)
from main.state import bot_start_time

IST = ZoneInfo("Asia/Kolkata")
UPDATE_INTERVAL = 10  # seconds
MAX_DURATION = 25 * 60  # 25 minutes in seconds
REMINDER_BEFORE = 60    # remind 1 min before stopping

# We store message IDs per chat so multiple users can use /stats independently
live_messages = {}  # chat_id → message_id

def close_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Close", callback_data="close_stats")]
    ])
def run_shell(cmd: str, timeout: int = 10) -> str:
    """Run shell command and return stripped stdout or error message"""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            executable="/bin/bash"  # better for complex commands
        )
        if result.returncode != 0:
            return f"Error: {result.stderr.strip() or 'unknown'}"
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "Error: command timed out"
    except Exception as e:
        return f"Error: {str(e)}"


def make_bar(perc: float, width: int = 26) -> str:
    perc = max(0, min(100, perc))
    filled = int(perc / 100 * width)
    return "█" * filled + "░" * (width - filled)


def get_system_stats() -> str:
    now = datetime.now(IST)
    now_str = now.strftime("%H:%M:%S")

    # ── RAM ────────────────────────────────────────────────
    ram_raw = run_shell("free | awk '/Mem:/ {printf \"Used: %.2f GiB (%.2f%%)\", $3/1024/1024, $3/$2*100}'")
    try:
        parts = ram_raw.split(" (")
        ram_line = parts[0]
        perc_str = parts[1].rstrip("%)")
        ram_perc = float(perc_str)
    except:
        ram_line = "Used: 1.51 GiB"
        ram_perc = 36.0
    ram_bar = make_bar(ram_perc)

    # ── Power (requires sudo rights or nopasswd for this path) ──
    start_raw = run_shell("sudo cat /sys/class/powercap/intel-rapl:0/energy_uj")
    time.sleep(1)
    end_raw = run_shell("sudo cat /sys/class/powercap/intel-rapl:0/energy_uj")

    try:
        start = int(start_raw)
        end = int(end_raw)
        watts = (end - start) / 1_000_000
        power_raw = f"{watts:.2f} W"
        power_perc = min(watts / 20.0 * 100, 100)
    except:
        power_raw = "0.00 W"
        power_perc = 0.0

    power_bar = make_bar(power_perc)

    # ── Network (accurate, no sar) ───────────────────────────
    # store previous values globally
    if not hasattr(get_system_stats, "_net"):
        get_system_stats._net = {"rx": 0, "tx": 0, "time": 0}

    def get_network_usage():
        net = get_system_stats._net

        try:
            with open("/proc/net/dev") as f:
                for line in f:
                    if "eth0:" in line:
                        data = line.split()
                        rx = int(data[1])
                        tx = int(data[9])
                        break
                else:
                    return 0.0, 0.0
        except:
            return 0.0, 0.0

        now = time.time()

        # first run (no previous data)
        if net["time"] == 0:
            net.update({"rx": rx, "tx": tx, "time": now})
            return 0.0, 0.0

        dt = now - net["time"]

        rx_rate = (rx - net["rx"]) / dt
        tx_rate = (tx - net["tx"]) / dt

        net.update({"rx": rx, "tx": tx, "time": now})

        return rx_rate / 1024, tx_rate / 1024  # KB/s


    rx_kb, tx_kb = get_network_usage()


    # ── Format nicely ───────────────────────────────────────
    def format_speed(kb):
        if kb < 1024:
            return f"{kb:.2f} KB/s", kb / 1024
        elif kb < 1024 * 1024:
            return f"{kb/1024:.2f} MB/s", kb / 1024
        else:
            return f"{kb/(1024*1024):.2f} GB/s", kb / 1024


    rx_str, rx_mb = format_speed(rx_kb)
    tx_str, tx_mb = format_speed(tx_kb)


    # ── Realistic scaling ───────────────────────────────────
    MAX_MBPS = 20.0  # adjust to your server bandwidth

    rx_perc = min((rx_mb / MAX_MBPS) * 100, 100)
    tx_perc = min((tx_mb / MAX_MBPS) * 100, 100)

    rx_bar = make_bar(rx_perc)
    tx_bar = make_bar(tx_perc)

    # ── Temperatures ───────────────────────────────────────
    temp_raw = run_shell(
        r"""sensors | awk '
            /Core 0:/ {printf "platform,coretemp -> %.1f\n", $3+0}
            /SYSTIN:/      {printf "platform,nct6779 -> %.0f\n", $2+0}
            /temp1:/       {printf "thermal,acpitz ->    %.0f\n", $2+0}
        '"""
    )
    temp_lines = []
    for line in temp_raw.splitlines():
        if not line.strip():
            continue
        try:
            _, val_str = line.rsplit(" ", 1)
            val = float(val_str)
            t_perc = min(val / 120.0 * 100, 100)
            t_bar = make_bar(t_perc)
            temp_lines.append(f"[{now_str}] {line} | {t_bar} {int(t_perc)}%")
        except:
            temp_lines.append(f"[{now_str}] {line}")
    temp_section = "\n".join(temp_lines) or f"[{now_str}] No temperature sensors found"

    # ── Disk ───────────────────────────────────────────────
    disk_used = run_shell("df -BG / | awk 'NR==2 {print $3}'") or "?"
    disk_avail = run_shell("df -BG / | awk 'NR==2 {print $4}'") or "?"
    disk_reserved_raw = run_shell("df / | awk 'NR==2 {r=$2-$3-$4; printf \"%.1f GiB\", r/1024/1024}'") or "?"
    try:
        used_p = int(run_shell("df / | awk 'NR==2 {print int($3/$2*100)}'") or 50)
        avail_p = int(run_shell("df / | awk 'NR==2 {print int($4/$2*100)}'") or 45)
        res_p = 100 - used_p - avail_p
    except:
        used_p = avail_p = res_p = 33
    disk_section = f"""Used: {disk_used}
{make_bar(used_p)} {used_p}%
Available: {disk_avail}
{make_bar(avail_p)} {avail_p}%
Reserved (root): {disk_reserved_raw}
{make_bar(res_p)} {res_p}%"""

    # ── Uptime & Bot ──────────────────────────────────────
    server_up_raw = run_shell(
        "awk '{d=int($1/86400); h=int(($1%86400)/3600); m=int(($1%3600)/60); printf \"%dd %02d:%02d\", d, h, m}' /proc/uptime"
    )
    server_up = server_up_raw or "?"

    bot_up_sec = int(time.time() - bot_start_time)
    bot_up = str(timedelta(seconds=bot_up_sec))
    bot_start_dt = datetime.fromtimestamp(bot_start_time, tz=IST).strftime("%d-%m-%Y %I:%M %p")

    # ── CPU ────────────────────────────────────────────────
    cpu_raw = run_shell(r"top -bn1 | grep '%Cpu(s)' | awk '{print 100-$8}'")
    try:
        cpu_p = float(cpu_raw)
    except:
        cpu_p = 11.0
    cpu_bar = make_bar(cpu_p)
    cpu_line = f"[{now_str}] CPU: {cpu_p:.1f}% | {cpu_bar}"

    # ── Fan ───────────────────────────────────────────────
    fan_raw = run_shell("sensors | awk '/fan2:/ {print $2}'")

    try:
        fan_val = float(fan_raw.strip())
    except:
        fan_val = 0.0

    MAX_FAN = 3000.0
    fan_perc = min((fan_val / MAX_FAN) * 100, 100)

    fan_bar = make_bar(fan_perc)
    
    # ── Voltage ────────────────────────────────────────────
    volt_raw = run_shell(
        r"""sensors | awk '
            /Vcore:/           {v = $2 + 0; if (v < 1500) v /= 1000; printf "Vcore: %.3f V\n", v}
            /AVCC:/            {printf "AVCC: %.2f V\n", $2 + 0}
            /\+3.3V:/          {printf "+3.3V: %.2f V\n", $2 + 0}
            /3VSB:/            {printf "3VSB: %.2f V\n", $2 + 0}
            /Vbat:/            {printf "Vbat: %.2f V\n", $2 + 0}
        '"""
    )

    volt_lines = []
    for line in volt_raw.splitlines():
        if not line.strip():
            continue
        try:
            name, v_str = line.split(" ", 1)
            v = float(v_str.rstrip("V").strip())
            v_perc = min(v / 3.5 * 100, 100)
            volt_lines.append(f"{name} {v_str} | {make_bar(v_perc)}")
        except:
            volt_lines.append(line)
            
    # ── Final formatted message ────────────────────────────
    dashboard = f"""{now.strftime('%b %d').upper()}

RAM——>
[{now_str}] {ram_line}
{ram_bar} {int(ram_perc)}%

Voltage
""" + "\n".join(volt_lines) + f"""

Power
[{now_str}] {power_raw} | {power_bar}

Temperature
{temp_section}

{disk_section}

Server Uptime: {server_up}
Bot Uptime: {bot_up}
Bot Started at: {bot_start_dt}

CPU
{cpu_line}

[{now_str}] Received: {rx_str} | {rx_bar} {int(rx_perc)}%
[{now_str}] Sent:     {tx_str} | {tx_bar} {int(tx_perc)}%

Sensor Fan Speed: Current ({fan_raw})
[{fan_raw}] {fan_bar} [3.000]
"""

    return f"**System Stats — {now.strftime('%Y-%m-%d %H:%M:%S IST')}**\n\n```\n{dashboard}\n```"

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stats command — send dashboard and start live updates"""
    chat_id = update.effective_chat.id

    # Delete previous message in this chat if exists
    old_msg_id = live_messages.get(chat_id)
    if old_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
        except:
            pass

    # Send initial message
    msg = await update.message.reply_text(
        get_system_stats(),
        parse_mode="Markdown",
        reply_markup=close_keyboard(),
        disable_notification=True
    )

    live_messages[chat_id] = msg.message_id
    context.application.create_task(
        live_update_task(context.application, chat_id, msg.message_id)
    )

async def close_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    msg_id = query.message.message_id

    try:
        await context.bot.delete_message(chat_id, msg_id)
    except:
        pass

    live_messages.pop(chat_id, None)
    
async def live_update_task(application, chat_id: int, message_id: int):
    """Background task that edits the stats message every UPDATE_INTERVAL seconds"""

    start_time = time.time()
    reminder_sent = False

    while True:
        await asyncio.sleep(UPDATE_INTERVAL)

        # If user manually closed
        if chat_id not in live_messages:
            break

        elapsed = time.time() - start_time

        # ⏰ Send reminder (only once)
        if not reminder_sent and elapsed >= (MAX_DURATION - REMINDER_BEFORE):
            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ Stats will be auto-paused in 1 minute. Press ❌ Close if you want to stop now."
                )
                reminder_sent = True
            except:
                pass

        # ⛔ Auto stop after 25 min
        if elapsed >= MAX_DURATION:
            try:
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="⏸️ Statistics paused automatically after 25 minutes.",
                )
            except:
                pass

            live_messages.pop(chat_id, None)
            break

        # 🔄 Normal update
        try:
            await application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=get_system_stats(),
                parse_mode="Markdown",
                reply_markup=close_keyboard()
            )
        except Exception as e:
            err = str(e).lower()

            if "message to edit not found" in err:
                live_messages.pop(chat_id, None)
                break

            if "not modified" in err:
                continue

            continue