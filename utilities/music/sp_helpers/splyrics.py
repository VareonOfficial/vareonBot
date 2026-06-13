# splyrics.py
import os
import re
import time
import hmac
import hashlib
import requests

# ─────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────

def extract_track_id(url: str):
    m = re.search(r"spotify\.com/track/([a-zA-Z0-9]+)", url)
    return m.group(1) if m else None


def sanitize(name: str) -> str:
    name = re.sub(r'[^\w\s-]', '', name)
    return re.sub(r'\s+', '_', name).strip('_')


# ─────────────────────────────────────────────
# TOTP
# ─────────────────────────────────────────────

SECRET_URL = "https://code.thetadev.de/ThetaDev/spotify-secrets/raw/branch/main/secrets/secretDict.json"

class TOTP:
    def __init__(self):
        r = requests.get(SECRET_URL, timeout=10)
        r.raise_for_status()
        data = r.json()
        ver = list(data.keys())[-1]
        arr = data[ver]
        transformed = [v ^ ((i % 33) + 9) for i, v in enumerate(arr)]
        key = "".join(str(x) for x in transformed)
        self.secret = key.encode()
        self.version = ver
        self.period = 30
        self.digits = 6

    def generate(self):
        counter = int(time.time() // self.period)
        counter_bytes = counter.to_bytes(8, "big")
        h = hmac.new(self.secret, counter_bytes, hashlib.sha1).digest()
        offset = h[-1] & 0x0F
        binary = (
            (h[offset] & 0x7F) << 24 |
            (h[offset+1] & 0xFF) << 16 |
            (h[offset+2] & 0xFF) << 8  |
            (h[offset+3] & 0xFF)
        )
        return str(binary % (10**self.digits)).zfill(self.digits)


# ─────────────────────────────────────────────
# Spotify Client
# ─────────────────────────────────────────────

class SpotifyClient:
    def __init__(self, sp_dc: str):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "Mozilla/5.0"})
        self.s.cookies.set("sp_dc", sp_dc)
        self.token = None
        self._auth()

    def _auth(self):
        totp = TOTP()
        code = totp.generate()

        params = {
            "reason": "init",
            "productType": "web-player",
            "totp": code,
            "totpVer": totp.version,
            "totpServer": code,
        }

        r = self.s.get("https://open.spotify.com/api/token", params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        self.token = data.get("accessToken")
        if not self.token:
            raise RuntimeError("Spotify auth failed")

    def get_lyrics(self, track_id: str):
        url = f"https://spclient.wg.spotify.com/color-lyrics/v2/track/{track_id}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "App-Platform": "WebPlayer",
        }
        params = {"format": "json", "market": "from_token"}

        r = self.s.get(url, headers=headers, params=params, timeout=12)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        raise RuntimeError(f"Lyrics API HTTP {r.status_code}")


# ─────────────────────────────────────────────
# LRC Writer
# ─────────────────────────────────────────────

def write_lrc(data: dict, out_dir: str, title, artists, album):
    if "lyrics" not in data:
        return False

    lyr = data["lyrics"]
    lines = lyr.get("lines", [])
    sync = lyr.get("syncType") == "LINE_SYNCED"

    title = title or "Unknown Title"

    if isinstance(artists, list):
        artist = ", ".join(artists)
    else:
        artist = artists or "Unknown Artist"

    album = album or "Unknown Album"

    os.makedirs(out_dir, exist_ok=True)
    fname = f"{sanitize(title)}.lrc"
    path = os.path.join(out_dir, fname)

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"[ti:{title}]\n")
        f.write(f"[ar:{artist}]\n")
        f.write(f"[al:{album}]\n")
        f.write("[by:Vareon]\n[offset:0]\n\n")

        if sync:
            for ln in lines:
                words = ln.get("words", "").strip()
                if not words:
                    continue
                ms = int(ln.get("startTimeMs", 0))
                mm = ms // 60000
                ss = (ms % 60000) // 1000
                xx = (ms % 1000) // 10
                f.write(f"[{mm:02d}:{ss:02d}.{xx:02d}]{words}\n")
        else:
            for ln in lines:
                words = ln.get("words", "").strip()
                if words:
                    f.write(words + "\n")

    return True


# ─────────────────────────────────────────────
# Public API for Telegram Bot
# ─────────────────────────────────────────────

def fetch_lyrics_and_write(track_url, sp_dc, output_dir, title, artists, album):
    track_id = extract_track_id(track_url)
    try:
        client = SpotifyClient(sp_dc)
        data = client.get_lyrics(track_id)
        if not data:
            return False
        return write_lrc(data, output_dir, title, artists, album)
    except Exception:
        return False