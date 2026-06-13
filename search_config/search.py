################################
# Search Handler
################################
import logging
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from telegram import Update, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from main.state import sessions
from .bollyflix import choose_bollyflix_result, extract_bollyflix_link, handle_fxlinks_page
from .animeflix import choose_animeflix_result, resolve_animeflix_listing
from .toonworld4all import choose_toonworld4all_result, extract_toonworld4all_link, extract_toonworld4all_link
from .moviesmod import choose_moviesmod_result, fetch_episode_links, fetch_host_links, process_single_link, resolve_modlist_domain
from .gamesleech import choose_gamesleech_result
from .katwap import choose_katwap_result, extract_katwap_link
from .constants import SEARCH_CHOICE, SEARCH_OPTION, SEARCH_RANGE, SAFE_TEXT_LIMIT, MAX_RESULTS, MAX_RESULTS_PER_SITE, safe_get
from .utils import build_final_link_buttons
MODLIST_TYPES = [
    "hollywood",
    "animeflix",
    "bollywood",
    "gamesleech",
]
EXTRA_SITES = {
    "ovagames": ("https://www.ovagames.com/", "path"),
    "bollyflix": ("https://new.bollyflix.gd/", "path"),
    "katwap":  ("https://katmoviehdd.com/", "story"),
    # "rareanimes": ("https://rareanimes.app", "query"),
    "toonworld4all": ("https://toonworld4all.me", "query"),
    # "apkmody": ("https://apkmody.com", "query"),
    "haxnode": ("https://haxnode.net", "query"),
    # "modyolo": ("https://modyolo.com", "query"),
    # "apkdone": ("https://apkdone.com", "query"),
    
}

SEARCH_PATTERNS = {
    "path": lambda base, q: f"{base}/search/{q}",
    "query": lambda base, q: f"{base}/?s={q}",
    "story":  lambda base, q: f"{base}/?do=search&subaction=search&story={q}",
}
def safe_truncate(text: str, limit: int = SAFE_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + "\n\n… truncated"

def domain_name(url):
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url
def build_search_message(results):
    lines = []
    for i, item in enumerate(results, 1):
        title = item.get("title", "Untitled")
        url = item.get("url", "")

        line = f"{i}. {title}\n{url}\n"
        lines.append(line)

        # Stop if nearing Telegram limit
        if sum(len(l) for l in lines) >= SAFE_TEXT_LIMIT:
            break

    text = "\n".join(lines)
    return safe_truncate(text)
def build_safe_keyboard(buttons, max_buttons=10):
    """
    buttons = [{"text": "...", "url": "..."}]
    """
    keyboard = []

    for btn in buttons[:max_buttons]:
        text = btn["text"][:60]   # Telegram-safe
        url = btn["url"]
        keyboard.append([InlineKeyboardButton(text=text, url=url)])

    return InlineKeyboardMarkup(keyboard)
def collect_search_results(query):
    seen = set()
    all_results = []

    for typ in MODLIST_TYPES:
        domain = resolve_modlist_domain(typ)
        if not domain:
            continue

        res = perform_search(domain, "path", query)
        for item in res:
            if item["url"] not in seen:
                seen.add(item["url"])
                all_results.append(item)

            if len(all_results) >= MAX_RESULTS:
                return all_results

    for _, (domain, pat) in EXTRA_SITES.items():
        res = perform_search(domain, pat, query)
        for item in res:
            if item["url"] not in seen:
                seen.add(item["url"])
                all_results.append(item)

            if len(all_results) >= MAX_RESULTS:
                return all_results

    return all_results

def perform_search(base_url, pattern, query):
    q = query.strip().replace(" ", "+")
    if not q:
        return []

    try:
        search_url = SEARCH_PATTERNS[pattern](base_url, q)
    except KeyError:
        logging.warning(f"Unknown search pattern: {pattern}")
        return []
    
    logging.info(f"[SEARCH] URL built: {search_url} (pattern={pattern})")
    r = safe_get(search_url)
    if not r:
        logging.warning(f"[SEARCH] Request failed for {search_url}")
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    query_l = query.lower()

    candidate_selectors = [
        "article h3 a",
        "h2 a",
        "h3 a",
        "div.post-title a",
        "header h2 a",
        "header h3 a",
    ]

    for selector in candidate_selectors:
        matches = soup.select(selector)
        for a in matches:
            title = a.get_text(strip=True)
            href = a.get("href")
            if title and href and query_l in title.lower():
                results.append({
                    "title": title,
                    "url": href,
                    "source": domain_name(base_url)
                })
                if len(results) >= MAX_RESULTS_PER_SITE:
                    break
        if len(results) >= MAX_RESULTS_PER_SITE:
            break

    # Fallback: scan all <a> tags
    if len(results) < MAX_RESULTS_PER_SITE:
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            href = a.get("href")
            if (
                text and href and query_l in text.lower()
                and 12 <= len(text) <= 120
                and not text.lower().startswith(("home", "login", "download", "click"))
            ):
                results.append({
                    "title": text,
                    "url": href,
                    "source": domain_name(base_url)
                })
                if len(results) >= MAX_RESULTS_PER_SITE:
                    break

    # Deduplicate
    seen_urls = set()
    unique = []
    for item in results:
        if item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            unique.append(item)

    logging.info(f"[SEARCH] Returning {len(unique)} unique results")
    return unique

    
async def start_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in sessions:
        await update.message.reply_text("❌ You are not logged in. Use /login first.")
        return ConversationHandler.END

    query = ' '.join(context.args).strip()

    if not query:
        await update.message.reply_text(
            "📌 **To use this feature**, send the name followed by /search.\n\n"
            "✅ **Example:**\n"
            "/search Panchayat\n"
            "/search Mirzapur\n\n"
            "This will start searching and show you download options.\n\n"
            "Please provide a movie/series name to continue! 🎬",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    # Temporary "Searching..." message
    searching_msg = await update.message.reply_text(
        f"🔍 Searching for: {query}... Please wait."
    )

    all_results = collect_search_results(query)

    if not all_results:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=searching_msg.message_id,
            text="😔 No results found for this search."
        )
        return ConversationHandler.END

    # Store results for next step
    context.user_data['all_results'] = all_results

    # ---------- build SAFE message ----------
    lines = ["<b>Found results:</b>\n"]
    for i, r in enumerate(all_results, 1):
        title = r.get("title", "Untitled").replace('`', '\\`')
        source = r.get("source", "")
        line = f"<b>{i}</b>. <blockquote>{title}</blockquote> — <i>{source}</i>\n"
        lines.append(line)

        # Stop early if nearing Telegram limit
        if sum(len(l) for l in lines) >= 3800:
            lines.append("\n…results truncated")
            break

    lines.append("\nReply with the <b>number</b> you want (or type <code>cancel</code>)")

    text = "".join(lines)

    # Send RESULTS as NEW message with ForceReply
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        parse_mode="HTML",
        reply_markup=ForceReply(
            selective=True,
            input_field_placeholder="Enter number (1, 2, ... or 'cancel')"
        ),
        reply_to_message_id=update.message.message_id
    )

    # Delete the temporary "Searching..." message
    try:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=searching_msg.message_id
        )
    except:
        pass

    return SEARCH_CHOICE

async def choose_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_input = update.message.text.strip()

    if text_input.lower() in ['cancel', 'skip']:
        await update.message.reply_text("Search cancelled.")
        return ConversationHandler.END

    if not text_input.isdigit():
        await update.message.reply_text("Please send a valid number.")
        return SEARCH_CHOICE

    idx = int(text_input) - 1
    results = context.user_data.get('all_results', [])

    if not (0 <= idx < len(results)):
        await update.message.reply_text("Invalid number. Try again.")
        return SEARCH_CHOICE

    selected = results[idx]
    context.user_data['selected_result'] = selected
    
    ###################################
    ### Branch depending on source site
    ####################################
    
    url = selected['url']
    if "bollyflix" in url:
        context.user_data['source_site'] = "bollyflix"
    elif "katwap" in url:
        context.user_data['source_site'] = "katwap"
    elif "animeflix" in url:
        context.user_data['source_site'] = "animeflix"
    elif "toonworld4all" in url:
        context.user_data['source_site'] = "toonworld4all"
    elif "gamesleech" in url:
        context.user_data['source_site'] = "gamesleech"
    else:
        context.user_data['source_site'] = "generic"
        
    source = context.user_data.get("source_site")

    if source == "bollyflix":
        return await choose_bollyflix_result(update, context, url)
    elif source == "katwap":
        return await choose_katwap_result(update, context, url)
    elif source == "animeflix":
        return await choose_animeflix_result(update, context, url)
    elif source == "toonworld4all":
        return await choose_toonworld4all_result(update, context, url)
    elif source == "gamesleech":
        return await choose_gamesleech_result(update.message, context, url)
    else:
        return await choose_moviesmod_result(update, context, url)


async def select_option(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    quality_msg_id = context.user_data.get('quality_msg_id')
    if quality_msg_id:
        try:
            await context.bot.delete_message(
                chat_id=query.message.chat_id,
                message_id=quality_msg_id
            )
            del context.user_data['quality_message_id']
        except Exception:
            pass
        context.user_data.pop('quality_msg_id', None)

    data = query.data
    if not data.startswith("opt_"):
        await query.message.reply_text("Invalid selection.")
        return SEARCH_OPTION

    _, major_str, minor_str = data.split("_")
    major = int(major_str)
    minor = int(minor_str)

    options = context.user_data['download_options']
    flat = context.user_data['flat_links']

    try:
        idx = sum(len(g['buttons']) for g in options[:major-1]) + (minor - 1)
        link = flat[idx]
    except Exception:
        await query.message.reply_text("Invalid option.")
        return SEARCH_OPTION

    source = context.user_data.get("source_site", "generic")
        
    ###################################
    ### Branch depending on source site
    ####################################
    
    if source == "bollyflix":
        processing_msg = await query.message.reply_text("⏳ Processing your link…")

        final = await extract_bollyflix_link(link)
        
        # Delete the processing message
        try:
            await processing_msg.delete()
        except:
            pass

        if not final:
            await query.message.reply_text("❌ Failed to extract Bollyflix link.")
            return ConversationHandler.END
        if "fxlinks" in final: 
            return await handle_fxlinks_page(query, context, final)

        reply_markup = await build_final_link_buttons(final)

        await query.message.reply_text(
            f"✅ Final link:\n{final}",
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )

        return ConversationHandler.END
    
    elif source == "katwap":
        processing_msg = await query.message.reply_text("⏳ Loading KatWap options…")

        await extract_katwap_link(query, context, link)

        # Delete processing message
        try:
            await processing_msg.delete()
        except:
            pass

        return ConversationHandler.END
    
    elif source == "toonworld4all":
        await query.message.reply_text(f"Selected Link:\n{link}")
        if any(x in link for x in [
            "archive.toonworld4all.me/redirect",
            "links.toonworld4all.me/redirect"
        ]):
            msg = await query.message.reply_text("⏳ Resolving final download link…")

            resolved = await extract_toonworld4all_link(link)

            if not resolved:
                await msg.edit_text("❌ Failed to bypass redirect.")
                return ConversationHandler.END

            await msg.edit_text(
                f"✅ Final link:\n{resolved}",
                disable_web_page_preview=True
            )
            return ConversationHandler.END

        final = extract_toonworld4all_link(link)

        if not final:
            await query.message.reply_text("❌ Failed to extract Toonworld4all link.")
            return ConversationHandler.END

        if final["type"] == "direct":
            msg = await query.message.reply_text("⏳ Resolving final download link…")

            resolved = await extract_toonworld4all_link(final["link"])

            if not resolved:
                await msg.edit_text("❌ Failed to bypass redirect.")
                return ConversationHandler.END

            await msg.edit_text(
                f"✅ Final link:\n{resolved}",
                disable_web_page_preview=True
            )

            return ConversationHandler.END


        # Case 2: archive episode with qualities
        if final["type"] == "qualities":
            context.user_data["toonworld_qualities"] = final["qualities"]
            context.user_data["toonworld_soup"] = final["soup"]

            text = "Available qualities:\n\n"
            keyboard = []

            for i, q in enumerate(final["qualities"], 1):
                text += f"{i}. {q['label']}\n"
                keyboard.append([
                    InlineKeyboardButton(q["label"], callback_data=f"tw_quality_{i}")
                ])

            await query.message.reply_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

            return SEARCH_OPTION

    elif source == "animeflix":
        processing_msg = await query.message.reply_text("⏳ Loading Animeflix content…")
        try:
            await processing_msg.delete()
        except:
            pass

        return await resolve_animeflix_listing(query.message, context, link)

    # Moviesmod flow
    episodes = fetch_episode_links(link)
    if episodes:
        avail = sorted(episodes.keys())
        await query.message.reply_text(
            f"🎞️Episodes found: {avail[0]} – {avail[-1]}\n\n"
            "Reply with range (e.g. 1-5) or single episode number"
        )
        context.user_data['episodes'] = episodes
        return SEARCH_RANGE

    hosts = fetch_host_links(link)
    if not hosts:
        await query.message.reply_text("No download links found.")
        return ConversationHandler.END

    chosen = hosts[0]['link']
    await process_single_link(query, context, chosen)
    return ConversationHandler.END


