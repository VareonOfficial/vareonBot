import re, logging
from typing import final
import requests
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import asyncio
import time
from playwright.async_api import async_playwright
from telegram import Update, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from .constants import SEARCH_OPTION, escape_markdown_v2, safe_get, TOONWORLD_QUALITY
from .utils import build_final_link_buttons
from main.config import CDP_URL
# from main.IamNOTaROBOT import check_verification
#######################################
# TOONWORLD4ALL DOWNLOAD PROCEDURE
#######################################
FINAL_HOSTS = ["filepress", "gdflix", "gdrive [tot]", "gdtot", "appdrive", "mega"]
PRIORITY = ("gdflix", "filepress", "gdrive [tot]", "gdtot", "appdrive", "mega")

def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()

def extract_mks_blocks(soup):
    blocks = []

    for block in soup.select("[class^=mks_]"):
        heading = block.select_one(
            ".mks_toggle_heading, .mks_accordion_heading"
        )
        content = block.select_one(
            ".mks_toggle_content, .mks_accordion_content"
        )

        if not heading or not content:
            continue

        label = clean(heading.get_text(" ", strip=True))

        # ❌ Skip screenshots
        if "screenshot" in label.lower():
            continue
        if content.find("img") and not content.find("a"):
            continue

        blocks.append((label, content))

    return blocks

def extract_best_host(container):
    found = []
    for a in container.find_all("a", href=True):
        logging.info(f"[TW] Anchor text='{a.get_text(strip=True)}' href='{a['href']}'")
        href = a["href"]
        text = a.get_text(strip=True).lower()
        logging.debug(f"[TW] Inspecting anchor → text='{text}' href='{href}'")

        for p in PRIORITY:
            if p in text or p in href.lower():
                logging.debug(f"[TW] Priority match: {p} (text='{text}', href='{href}')")
                found.append((PRIORITY.index(p), href))
                break

    if not found:
        logging.debug("[TW] No priority host found in this block")
        return None

    found.sort(key=lambda x: x[0])
    chosen = found[0][1]
    logging.info(f"[TW] Best host selected: {chosen}")
    return chosen


def classify_block(label, content):
    logging.debug(f"[TW] Classifying block: {label}")
    links = content.find_all("a", href=True)
    logging.debug(f"[TW] Found {len(links)} anchors in block")

    # Episode / Movie page
    for a in links:
        href = a["href"]
        text = a.get_text(strip=True).lower()
        logging.debug(f"[TW] Checking episode/movie anchor → text='{text}' href='{href}'")

        if (
            "watch" in text
            or "/episode/" in href
            or "/movie/" in href
        ):
            logging.info(f"[TW] Classified as EPISODE: {label} → {href}")
            return {
                "type": "episode",
                "label": label,
                "link": href
            }

    # ZIP / Direct hosts
    best = extract_best_host(content)
    if best:
        logging.info(f"[TW] Classified as ZIP: {label} → {best}")
        return {
            "type": "zip",
            "label": label,
            "link": best
        }

    logging.debug(f"[TW] Block {label} did not match any classification")
    return None


def process_block(heading, content, grouped, seen):
    label = clean(heading.get_text(" ", strip=True))
    if not label or label in seen:
        return

    # ❌ Skip screenshot-only blocks
    if "screenshot" in label.lower():
        return
    if content.find("img") and not content.find("a"):
        return

    links = content.find_all("a", href=True)
    if not links:
        return

    # ── Case 1: Episode / Movie page (Watch / Download)
    for a in links:
        href = a["href"]
        text = a.get_text(strip=True).lower()

        if (
            "watch" in text
            or "/episode/" in href
            or "/movie/" in href
        ):
            grouped.append({
                "type": "episode",
                "label": label,
                "buttons": [
                    {
                        "text": "Watch / Download",
                        "link": href
                    }
                ]
            })
            seen.add(label)
            return

    # ── Case 2: ZIP / Direct hosts → auto-pick by priority
    best_link = extract_best_host(content)
    if best_link:
        grouped.append({
            "type": "zip",
            "label": label,
            "buttons": [
                {
                    "text": "Download",
                    "link": best_link
                }
            ]
        })
        seen.add(label)

def fetch_toonworld4all_options(url, safe_get):
    r = safe_get(url)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    options = []

    for label, content in extract_mks_blocks(soup):
        info = classify_block(label, content)
        if info:
            options.append(info)

    return options

async def choose_toonworld4all_result(update, context, url):
    options = fetch_toonworld4all_options(url, safe_get)
    if not options:
        await update.message.reply_text("No download options found.")
        return ConversationHandler.END

    context.user_data["toonworld_options"] = options

    text = "*Available options:*\n\n"
    keyboard = []

    z = e = 1

    for i, opt in enumerate(options):
        if opt["type"] == "zip":
            text += f"*Z{z}* {escape_markdown_v2(opt['label'])}\n"
            keyboard.append([
                InlineKeyboardButton(
                    text=f"Z{z}",
                    callback_data=f"tw_opt_{i}"
                )
            ])
            z += 1

    if z > 1:
        text += "\n"

    for i, opt in enumerate(options):
        if opt["type"] == "episode":
            text += f"*E{e}* {escape_markdown_v2(opt['label'])}\n"
            keyboard.append([
                InlineKeyboardButton(
                    text=f"E{e}",
                    callback_data=f"tw_opt_{i}"
                )
            ])
            e += 1

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="MarkdownV2"
    )
    return SEARCH_OPTION

def extract_toonworld4all_link(page_url):
    BASE = "https://archive.toonworld4all.me"

    if "/redirect/" in page_url:
        return {"type": "direct", "link": page_url}

    r = safe_get(page_url)
    if not r:
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    # ─────────────────────────────
    # CASE 1: FINAL HOST PAGE
    # ─────────────────────────────
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        href = a["href"]

        for host in PRIORITY:
            if host in text or host in href.lower():
                return {"type": "direct", "link": href}

    # ─────────────────────────────
    # CASE 2: ARCHIVE QUALITY PAGE
    # ─────────────────────────────
    qualities = []

    for block in soup.select("div.bg-muted, div.hover\\:bg-muted"):
        h3 = block.find("h3")
        if not h3:
            continue

        label = h3.get_text(strip=True)
        qualities.append({
            "label": label,
            "block": block
        })

    if qualities:
        return {
            "type": "qualities",
            "qualities": qualities,
            "soup": soup
        }

    return None

def choose_quality_and_mirror(soup):
    mirrors = []

    for card in soup.select("div.border-r.border-b"):
        title_tag = card.find("h4")
        link_tag = card.find("a", href=True)
        if not title_tag or not link_tag:
            continue

        title = title_tag.get_text(strip=True).lower()
        href = link_tag["href"]
        full_link = urljoin("https://archive.toonworld4all.me", href)

        mirrors.append({"text": title, "link": full_link})

    if not mirrors:
        return None

    for keyword in (PRIORITY):
        for m in mirrors:
            if keyword in m["text"]:
                return m

    return mirrors[0]

async def handle_toonworld4all_quality(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    logging.info(f"[TW] Callback received: {query.data}")

    # ── Parse index
    try:
        _, _, i = query.data.split("_")
        i = int(i)
    except Exception:
        logging.exception("[TW] Failed to parse callback data")
        await query.message.reply_text("❌ Invalid selection.")
        return ConversationHandler.END

    options = context.user_data.get("toonworld_options")

    if not options or i < 0 or i >= len(options):
        logging.error("[TW] Option index out of range")
        await query.message.reply_text("❌ Invalid selection.")
        return ConversationHandler.END

    selected = options[i]
    label = selected["label"]

    logging.info(f"[TW] Selected option: {label} ({selected['type']})")

    # ─────────────────────────────
    # ZIP → bypass immediately
    # ─────────────────────────────
    if selected["type"] == "zip":
        msg = await query.message.reply_text("⏳ Resolving download…")
        logging.info(f"Processing link: {selected['link']}")

        resolved = await extract_toonworld4all_link(selected["link"])
        if not resolved:
            await msg.edit_text("❌ Failed to resolve download.")
            return ConversationHandler.END

        await msg.edit_text(
            f"✅ Final link:\n{resolved}",
            disable_web_page_preview=True
        )
        logging.info("[TW] ZIP flow completed")
        return ConversationHandler.END

    # ─────────────────────────────
    # EPISODE / MOVIE → quality selection
    # ─────────────────────────────
    info = extract_toonworld4all_link(selected["link"])

    if not info or info["type"] != "qualities":
        logging.error("[TW] Quality data missing")
        await query.message.reply_text("❌ Could not load qualities.")
        return ConversationHandler.END

    keyboard = []
    text = "*Select quality:*\n\n"

    for idx, q in enumerate(info["qualities"], start=1):
        safe = escape_markdown_v2(q["label"])
        text += f"*[{idx}]* {safe}\n"
        keyboard.append([
            InlineKeyboardButton(
                text=q["label"],
                callback_data=f"tw_quality_{idx-1}"
            )
        ])

    context.user_data["toonworld_quality_soup"] = info["soup"]
    context.user_data["toonworld_qualities"] = info["qualities"]

    await query.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="MarkdownV2"
    )

    logging.info("[TW] Quality selection shown")
    return TOONWORLD_QUALITY

async def handle_toonworld_quality_choice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    logging.info("=" * 60)
    logging.info("[TW] Quality selection callback received")
    logging.info(f"[TW] Callback data: {query.data}")
    logging.info(f"[TW] User ID: {update.effective_user.id}")

    # ─────────────────────────────
    # Parse callback index
    # ─────────────────────────────
    try:
        _, _, idx = query.data.split("_")
        idx = int(idx)
        logging.info(f"[TW] Parsed quality index: {idx}")
    except Exception as e:
        logging.exception("[TW] Failed to parse callback data")
        await query.message.reply_text("❌ Invalid quality selection.")
        return ConversationHandler.END

    # ─────────────────────────────
    # Validate stored qualities
    # ─────────────────────────────
    qualities = context.user_data.get("toonworld_qualities")

    if not qualities:
        logging.error("[TW] toonworld_qualities missing from user_data")
        await query.message.reply_text("❌ Quality data missing.")
        return ConversationHandler.END

    logging.info(f"[TW] Total qualities available: {len(qualities)}")

    if idx < 0 or idx >= len(qualities):
        logging.error(f"[TW] Quality index out of range: {idx}")
        await query.message.reply_text("❌ Invalid quality selection.")
        return ConversationHandler.END

    chosen_quality = qualities[idx]
    label = chosen_quality.get("label", "UNKNOWN")

    logging.info(f"[TW] Selected quality label: {label}")

    # ─────────────────────────────
    # Get full soup (IMPORTANT)
    # ─────────────────────────────
    soup = context.user_data.get("toonworld_quality_soup")

    if not soup:
        logging.error("[TW] Full soup missing from user_data")
        await query.message.reply_text("❌ Internal error (soup missing).")
        return ConversationHandler.END

    # ─────────────────────────────
    # Locate quality heading in soup
    # ─────────────────────────────
    heading = soup.find(
        "h3",
        string=lambda s: s and label.lower() in s.lower()
    )

    if not heading:
        logging.error(f"[TW] Quality heading not found in soup: {label}")
        await query.message.reply_text("❌ Quality section not found.")
        return ConversationHandler.END

    logging.info("[TW] Quality heading found")
    logging.debug(f"[TW] Heading HTML:\n{heading.prettify()}")

    # ─────────────────────────────
    # Locate mirror container
    # ─────────────────────────────
    container = heading.find_parent()

    if not container:
        logging.error("[TW] Heading parent container not found")
        await query.message.reply_text("❌ Mirror container missing.")
        return ConversationHandler.END

    logging.info("[TW] Heading parent container found")

    # Try to find mirror cards AFTER heading
    cards = container.find_all_next(
        "div",
        class_="border-r border-b"
    )
    # ─────────────────────────────
    # Extract mirrors (GLOBAL PANEL)
    # ─────────────────────────────
    logging.info("[TW] Extracting mirrors from global mirror panel")

    soup = context.user_data.get("toonworld_quality_soup")

    cards = soup.select("div.border-r.border-b")

    logging.info(f"[TW] Total mirror cards found globally: {len(cards)}")

    mirrors = []

    for i, card in enumerate(cards, start=1):
        logging.info(f"[TW] Inspecting mirror card #{i}")

        title_tag = card.find("h4")
        link_tag = card.find("a", href=True)

        if not title_tag or not link_tag:
            logging.warning("[TW] Skipped card (missing title or link)")
            continue

        title = title_tag.get_text(strip=True).lower()
        href = link_tag["href"]

        logging.info(f"[TW] Mirror title: {title}")
        logging.info(f"[TW] Mirror href: {href}")

        if "/redirect/" not in href:
            logging.warning("[TW] Skipped non-redirect link")
            continue

        mirrors.append({
            "text": title,
            "link": urljoin("https://archive.toonworld4all.me", href)
        })

    logging.info(f"[TW] Valid mirrors collected: {len(mirrors)}")

    if not mirrors:
        logging.error("[TW] No mirrors found after extraction")
        await query.message.reply_text("❌ No mirror found for this quality.")
        return ConversationHandler.END

    # ─────────────────────────────
    # Select mirror by priority
    # ─────────────────────────────
    
    final_mirror = None

    for p in PRIORITY:
        for m in mirrors:
            if p in m["text"] or p in m["link"]:
                final_mirror = m
                logging.info(f"[TW] Priority mirror selected: {p}")
                break
        if final_mirror:
            break

    if not final_mirror:
        final_mirror = mirrors[0]
        logging.info("[TW] No priority match, using first mirror")

    final_link = final_mirror["link"]

    logging.info(f"[TW] Final mirror link: {final_link}")
    
    # ─────────────────────────────
    # Notify user and bypass
    # ─────────────────────────────

    progress_msg = await query.message.reply_text(
        "⏳ Resolving final download link…",
        disable_web_page_preview=True
    )

    resolved = await extract_toonworld4all_link(final_link)

    # Always try to delete the progress message
    try:
        await progress_msg.delete()
    except Exception:
        pass  # message may already be gone; ignore safely

    if not resolved:
        await query.message.reply_text("❌ Failed to bypass mirror.")
        return ConversationHandler.END

    logging.info(f"[TW] Final resolved link: {resolved}")

    keyboard = await build_final_link_buttons(resolved)

    await query.message.reply_text(
        f"✅ Final link:\n{resolved}",
        reply_markup=keyboard,
        disable_web_page_preview=True
    )

    logging.info("[TW] Quality flow completed successfully")
    logging.info("=" * 60)

    return ConversationHandler.END


###########BY-PASSING LOGIC###########
######################################

async def bypass_link(page):
    for attempt in range(10):
        logging.info(f"Bypass attempt {attempt+1}")

        await page.evaluate("""
            document.querySelectorAll(".adrino-pop, .adb, #adb").forEach(el => el.remove());
            document.body.style.overflow = "auto";
            document.documentElement.style.overflow = "auto";

            ['#nextbtn', '#tp-snp2'].forEach(sel => {
                const btn = document.querySelector(sel);
                if (btn) {
                    btn.disabled = false;
                    btn.removeAttribute('disabled');
                    btn.style.pointerEvents = 'auto';
                    btn.style.opacity = '1';
                }
            });

            window.setTimeout = () => {};
            window.setInterval = () => 0;

            ['#nextbtn', '#tp-snp2'].forEach(sel => {
                const btn = document.querySelector(sel);
                if (btn && typeof btn.onclick === 'function') {
                    btn.onclick();
                }
            });
        """)

        btn = await page.query_selector("#tp-snp2")
        if btn:
            await btn.click()
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except:
                await page.wait_for_timeout(2000)

        current_url = page.url
        logging.info(f"Current URL: {current_url}")

        if any(h in current_url for h in FINAL_HOSTS):
            logging.info("✅ Final host reached")
            return page.url

    return None

async def extract_toonworld4all_link(start_url: str) -> str | None:
    command = ["vsearch", "--type", "toonworld4all", start_url]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
)
    stdout, stderr = await process.communicate()
    output = stdout.decode() + stderr.decode()
    url_match = re.search(r"URL:\s+(https?://[^\s]+)", output)
    if url_match:
        return url_match.group(1).strip()
    else:
        print(f"DEBUG: vsearch output was: {output}")
        return None