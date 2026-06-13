import requests
from urllib.parse import urlparse
from config import HEADERS, logger

# The target hosts we are looking for
FINAL_HOSTS = ["gdflix", "driveseed", "filepress", "hubcloud", "fxlinks"]

def is_final_host(url):
    """Checks if the current URL domain contains any of the target hosts."""
    if not url:
        return False
    domain = urlparse(url).netloc.lower()
    return any(host in domain for host in FINAL_HOSTS)

def process_url(url):
    """Processes URL by following redirects to the final host."""
    session = requests.Session()

    try:
        # Step 1: Initial request to follow standard redirects
        r1 = session.get(
            url,
            headers=HEADERS,
            allow_redirects=True,
            timeout=30
        )
        if is_final_host(r1.url):
            return r1.url

        logger.warning(f"⚠️ Reached unknown host: {r1.url}")
        return None

    except Exception as e:
        logger.error(f"❌ Bollyflix Error: {e}")
        return None