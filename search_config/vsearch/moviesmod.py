import sys, re, logging
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
from config import HEADERS, logger

def is_driveseed_url(url):
    return url and "driveseed" in url


def resolve_driveseed_response(response):
    if not is_driveseed_url(response.url):
        return None

    if "/file/" in response.url:
        return response.url

    soup = BeautifulSoup(response.text, "html.parser")

    a = soup.find("a", href=True)
    if a and "/file/" in a["href"]:
        return urljoin("https://driveseed.org", a["href"])

    meta = soup.find("meta", attrs={"http-equiv": "refresh"})
    if meta:
        content = meta.get("content", "")
        match = re.search(r'url=(https?://[^"\']+)', content, re.I)
        if match:
            return match.group(1)

    for script in soup.find_all("script"):
        if not script.string:
            continue

        match = re.search(
            r'(?:window\.location(?:\.href)?|location\.replace)\s*\(\s*[\'\"]([^\'\"]+)[\'\"]\s*\)',
            script.string
        )
        if match:
            link = match.group(1)
            if link.startswith("/"):
                return urljoin("https://driveseed.org", link)
            return link

    return None

def extract_form(html):
    soup = BeautifulSoup(html, "html.parser")

    form = soup.find("form")

    if not form:
        return None, None

    action = form.get("action")

    data = {}

    for inp in form.find_all("input"):
        name = inp.get("name")
        value = inp.get("value", "")

        if name:
            data[name] = value

    return action, data


def submit_form(session, action, data, referer=None):

    headers = {
        **HEADERS,
        "content-type": "application/x-www-form-urlencoded"
    }

    if referer:
        headers["referer"] = referer

    r = session.post(
        action,
        data=data,
        headers=headers,
        allow_redirects=True,
        timeout=30
    )

    return r


def process_url(url):
    session = requests.Session()

    r1 = session.get(
        url,
        headers=HEADERS,
        allow_redirects=True,
        timeout=30
    )
    resolved = resolve_driveseed_response(r1)
    if resolved:
        return resolved
    action1, data1 = extract_form(r1.text)
    if not action1:
        logger.error("❌ No form found in STEP 1")
        return None

    r2 = submit_form(
        session=session,
        action=action1,
        data=data1,
        referer=r1.url
    )
    resolved = resolve_driveseed_response(r2)
    if resolved:
        return resolved
    action2, data2 = extract_form(r2.text)

    if not action2:
        logger.error("❌ No second form found")
        return None

    r3 = submit_form(
        session=session,
        action=action2,
        data=data2,
        referer=r2.url
    )
    resolved = resolve_driveseed_response(r3)
    if resolved:
        return resolved

    go_match = re.search(
        r'https://[^"\']+\?go=[^"\']+',
        r3.text
    )

    if not go_match:
        logger.error("❌ GO URL NOT FOUND")
        return None

    go_url = go_match.group(0)
    extracted_domain = urlparse(go_url).netloc

    cookie_match = re.search(
        r"s_343\('([^']+)',\s*'([^']+)'",
        r3.text
    )

    if not cookie_match:
        logger.error("❌ COOKIE NOT FOUND")
        return None

    cookie_name = cookie_match.group(1)
    cookie_value = cookie_match.group(2)

    session.cookies.set(
        cookie_name,
        cookie_value,
        domain=extracted_domain
    )

    r4 = session.get(
        go_url,
        headers=HEADERS,
        allow_redirects=True,
        timeout=30
    )
    resolved = resolve_driveseed_response(r4)
    if resolved:
        return resolved
    meta_match = re.search(
        r'url=(https://[^"]+)',
        r4.text
    )

    if not meta_match:
        logger.error("❌ META REFRESH URL NOT FOUND")
        return None

    redirect_url = meta_match.group(1)

    r5 = session.get(
        redirect_url,
        headers=HEADERS,
        allow_redirects=True,
        timeout=30
    )
    resolved = resolve_driveseed_response(r5)
    if resolved:
        return resolved
    return r5.url