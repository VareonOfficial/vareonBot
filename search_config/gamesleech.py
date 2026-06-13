import logging
import requests
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ConversationHandler
from .constants import GAMELEECH_PARTS, GAMELEECH_CHOICE
import asyncio
from playwright.async_api import async_playwright, Page
from .utils import parse_driveseed_buttons, build_driveseed_keyboard
from main.config import CDP_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

FINAL_HOSTS = ["filepress", "gdflix", "gdtot", "appdrive", "driveseed"]

AD_DOMAINS = [
    "acscdn.com",
    "static.cloudflareinsights.com",
    "googletagmanager.com",
    "usrpubtrk.com"
]

def fetch_gamesleech_options(url):
    r = requests.get(url, timeout=15)
    if not r.ok:
        logging.error(f"[GL] Failed to fetch {url}")
        return {}

    soup = BeautifulSoup(r.text, "html.parser")

    # 🔒 LIMIT TO FIRST ACCORDION ONLY
    accordion = soup.select_one(".su-accordion")
    if not accordion:
        logging.error("[GL] No accordion found")
        return {}

    options = {}

    # ── ZIP (file-spoiler only)
    zip_block = accordion.select_one(
        ".file-spoiler .download_file a[href]"
    )
    if zip_block:
        options["zip"] = {
            "label": zip_block.get_text(strip=True),
            "link": zip_block["href"]
        }
        logging.info(f"[GL] ZIP found: {options['zip']['label']}")

    # ── PARTS (parts-spoiler only)
    part_blocks = accordion.select(
        ".parts-spoiler .download_part a[href]"
    )
    if part_blocks:
        parts = []
        for idx, a in enumerate(part_blocks, start=1):
            parts.append({
                "index": idx,
                "label": a.get_text(strip=True),
                "link": a["href"]
            })
        options["parts"] = parts
        logging.info(f"[GL] Parts found: {len(parts)}")

    return options

async def choose_gamesleech_result(message, context, link):
    options = fetch_gamesleech_options(link)

    if not options:
        await message.reply_text("❌ Failed to extract Gamesleech options.")
        return ConversationHandler.END

    context.user_data["gamesleech_options"] = options

    text = "Available options:\n\n"
    keyboard = []
    next_state = ConversationHandler.END

    # ── PARTS
    if "parts" in options:
        total = len(options["parts"])
        text += (
            f"📦 Download as Parts: 1 – {total}\n"
            "Reply with range (e.g. 1-5) or indices (e.g. 2,4).\n\n"
        )
        context.user_data["gamesleech_parts"] = options["parts"]
        next_state = GAMELEECH_PARTS

    # ── ZIP
    if "zip" in options:
        text += "OR\n\n⬇ Download ZIP"
        keyboard.append([
            InlineKeyboardButton(
                "⬇ Download ZIP",
                callback_data="GAMELEECH_ZIP"
            )
        ])
        # if parts exist, parts take priority; otherwise ZIP state
        if next_state == ConversationHandler.END:
            next_state = GAMELEECH_CHOICE

    await context.bot.send_message(
        chat_id=message.chat.id,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
        disable_web_page_preview=True,
        reply_to_message_id=message.message_id
    )

    return next_state

async def handle_gamesleech_zip(update, context):
    query = update.callback_query
    await query.answer()

    zip_option = context.user_data.get("gamesleech_options", {}).get("zip")
    if not zip_option:
        await query.edit_message_text("❌ ZIP option not found.")
        return ConversationHandler.END
    
# Send processing message
    processing_msg = await context.bot.send_message(
        chat_id=query.message.chat.id,
        text="⏳ Processing ZIP...",
        disable_web_page_preview=True,
        reply_to_message_id=query.message.message_id
    )

    final_link = await extract_gamesleech_link(zip_option["link"])
    actions = parse_driveseed_buttons(final_link)
    keyboard = build_driveseed_keyboard(actions)

    await query.edit_message_text(
        f"✅ ZIP Selected\n\nFinal Link:\n{final_link}",
        disable_web_page_preview=True,
        reply_markup=keyboard
    )

    # Delete the processing message after editing
    await context.bot.delete_message(
        chat_id=query.message.chat.id,
        message_id=processing_msg.message_id
    )

    return ConversationHandler.END


def parse_part_selection(text, max_index):
    selected = set()

    for token in text.replace(" ", "").split(","):
        if "-" in token:
            start, end = token.split("-", 1)
            if start.isdigit() and end.isdigit():
                for i in range(int(start), int(end) + 1):
                    if 1 <= i <= max_index:
                        selected.add(i)
        elif token.isdigit():
            i = int(token)
            if 1 <= i <= max_index:
                selected.add(i)

    return sorted(selected)

async def handle_gamesleech_parts(update, context):
    message = update.message
    user_input = message.text.strip()

    parts = context.user_data.get("gamesleech_parts")
    if not parts:
        await message.reply_text("❌ Parts data missing.")
        return ConversationHandler.END

    selected_indices = parse_part_selection(user_input, len(parts))
    if not selected_indices:
        await message.reply_text("❌ Invalid selection. Try again.")
        return GAMELEECH_PARTS

    status_msg = await message.reply_text(
        "⏳ Processing selected parts...\n",
        disable_web_page_preview=True
    )

    output_lines = []

    for idx in selected_indices:
        part = parts[idx - 1]

        # send to extractor
        final_link = await extract_gamesleech_link(part["link"])
        actions = parse_driveseed_buttons(final_link)
        output_lines.append(
            f"📦 Processing Part {idx}\nFinal Link:\n{final_link}\n"
        )
        if "telegram" in actions:
            progress_text += f"Telegram File - {actions['telegram']}\n\n"

        # edit the SAME message
        await status_msg.edit_text(
            "\n".join(output_lines),
            disable_web_page_preview=True
        )

    return ConversationHandler.END

async def block_ads(route):
    url = route.request.url
    if any(domain in url for domain in AD_DOMAINS):
        logging.info(f"Blocked ad request: {url}")
        await route.abort()
    else:
        await route.continue_()


async def bypass_verify_page(page: Page) -> str | None:
    current_url = page.url
    if any(h in current_url.lower() for h in FINAL_HOSTS):
        logging.info(f"✅ Already at final host: {current_url}")
        return current_url
    logging.info(f"▶ Step 1: Force-unhide and click 'Click to Verify' on: {current_url}")

    await page.wait_for_selector(
        "button.wp-block-button__link",
        state="attached",
        timeout=20000
    )

    await page.evaluate("""
        const btn = document.querySelector('button.wp-block-button__link');
        if (btn) {
            btn.style.display = 'inline-block';
            btn.style.visibility = 'visible';
            btn.style.opacity = '1';
            btn.removeAttribute('disabled');

            btn.scrollIntoView({ behavior: 'smooth', block: 'center' });

            btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
            btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
            btn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        }
    """)

    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(2000)


    # ─────────────────────────────────────

    logging.info("▶ Step 2: Wait for hidden 'Click Here To Continue'")

    await page.wait_for_selector(
        "#verify_button",
        state="attached",
        timeout=20000
    )

    logging.info("▶ Step 3: Unhide and click 'Click Here To Continue'")

    await page.evaluate("""
        const btn = document.querySelector('#verify_button');
        if (btn) {
            btn.style.display = 'inline-block';
            btn.style.visibility = 'visible';
            btn.removeAttribute('disabled');

            btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
            btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
            btn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        }
    """)

    # Allow ad tab to open & JS to unlock
    await page.wait_for_timeout(4000)

    # ─────────────────────────────────────

    logging.info("▶ Step 4: Wait for 'Go to Download' button to unlock")
    await page.evaluate("""
        const btn = document.querySelector('#verify_button');
        if (btn) {
            btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
            btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
            btn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        }
    """)

    await page.wait_for_selector(
        "#two_steps_btn",
        state="attached",
        timeout=30000
    )

    # Wait until button is actually usable
    await page.wait_for_function("""
        const btn = document.querySelector('#two_steps_btn');
        btn &&
        btn.offsetParent !== null &&
        !btn.hasAttribute('disabled');
    """, timeout=30000)

    final_link = None

    for attempt in range(1, 6):
        logging.info(f"▶ Attempt {attempt}: Clicking 'Go to Download'")

        await page.evaluate("""
            const btn = document.querySelector('#two_steps_btn');
            if (btn) {
                btn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                btn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
            }
        """)

        await page.wait_for_timeout(4000)

        current_url = page.url
        logging.info(f"▶ Current URL: {current_url}")

        if any(h in current_url.lower() for h in FINAL_HOSTS):
            logging.info("✅ Final host reached")
            return current_url

        logging.warning("⚠ Still not on final host, retrying...")

    logging.warning("⚠ Final host not reached after retries")
    return page.url


async def extract_gamesleech_link(url: str) -> str | None:
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        context = await browser.new_context()
        
        main_page = None
        def on_new_page(p):
            nonlocal main_page
            if main_page is None:
                main_page = p
            else:
                asyncio.create_task(p.close())

        context.on("page", on_new_page)
        page = await context.new_page()
        
        try:
            # Initial navigation
            await page.goto(url, wait_until="domcontentloaded")

            # Run the bypass logic
            final = await bypass_verify_page(page)

            if not final:
                await context.close()
                return None

            logging.info(f"▶ Verification finished. Monitoring redirects for: {final}")
            
            for i in range(1, 11):
                current = page.url.lower()

                if any(h in current for h in FINAL_HOSTS):
                    logging.info("="*50)
                    logging.info(f"✅ SUCCESS: Final host reached on attempt {i}")
                    logging.info(f"🔗 FINAL LINK: {page.url}")
                    logging.info("="*50)
                    
                    final_url = page.url
                    await context.close()
                    return final_url

                logging.info(f"⏳ Waiting for redirect... Current: {current[:50]}...")
                await page.wait_for_timeout(2000)

            logging.warning("⚠ Final host not reached after 10 redirect checks.")
            final_url = page.url
            await context.close() 
            return final_url

        except Exception as e:
            logging.error(f"❌ Extraction Error: {e}")
            await context.close()
            return None