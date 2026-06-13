import requests
import re
import logging

# ====================== CONFIG ======================
HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}

logger = logging.getLogger(__name__)

# ====================== HELPER FUNCTIONS ======================

def extract_adrino_id(html: str):
    """Extract the link ID from various possible patterns"""
    patterns = [
        r'adrino1\.carrnissan\.com/safe\.php\?link=([a-zA-Z0-9]+)',
        r'adrinolinks\.com/([a-zA-Z0-9]+)',
        r'safe\.php\?link=([a-zA-Z0-9]+)',
        r'link=([a-zA-Z0-9]{6,})',                    # generic fallback
    ]
    
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def get_with_headers(session, url, referer, cookie=None, extra_headers=None):
    """Reusable GET with custom headers"""
    headers = HEADERS_BASE.copy()
    headers["Referer"] = referer
    
    if cookie:
        headers["Cookie"] = cookie
    if extra_headers:
        headers.update(extra_headers)
    
    return session.get(url, headers=headers, allow_redirects=True, timeout=30)


# ====================== MAIN PROCESS ======================

def process_url(url: str):
    session = requests.Session()
    
    # ==================== STEP 1: First Request (archive.toonworld4all) ====================
    print(f"[+] Step 1: Accessing -> {url}")
    
    # The initial request to the ToonWorld4All URL, which should redirect to the adrino link
    r1 = get_with_headers(
        session=session,
        url=url,
        referer="https://toonworld4all.me/",
        # index=2 means that it will redirect to the adrino link
        cookie="shortener_index=2; user_system_preference=manual"
    )

    print(f"[+] Landed on: {r1.url}")

    # Extract the link ID, for example from a pattern like "adrino1.carrnissan.com/safe.php?link=abc123"
    link_id = extract_adrino_id(r1.text)
    if not link_id:
        logger.error("[-] Could not extract link ID")
        print("[-] Response snippet:", r1.text[:600])
        return None
    
    # Extracted link ID for example "abc123"
    print(f"[+] Extracted Link ID: {link_id}")

    # ==================== STEP 2: Open.php Request ====================
    open_url = f"https://adrino.carrnissan.com/includes/open.php?id={link_id}"
    
    print(f"[+] Step 2: Accessing -> {open_url}")
    # Open the URL with appropriate cookies and headers to get the final redirected URL
    r2 = get_with_headers(
        session=session,
        url=open_url,
        referer="https://adrino.carrnissan.com/",
        cookie=f"open={link_id}",
        extra_headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        }
    )
    # Final link
    return r2.url
