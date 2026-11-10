"""SaveReel — paste a public Facebook video/reel link, get a downloadable MP4.

The server never stores videos: it asks yt-dlp for the video's direct MP4 URL,
then streams the bytes straight through to the visitor's browser.
"""
import asyncio
import json
import logging
import re
from html import unescape
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ALLOWED_PAGE_HOSTS = ("facebook.com", "fb.watch", "fb.com")
ALLOWED_CDN_SUFFIX = ".fbcdn.net"
CACHE_TTL = 15 * 60          # seconds an extracted result stays cached
RATE_LIMIT = 20              # requests per IP ...
RATE_WINDOW = 10 * 60        # ... per 10 minutes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("savereel")

app = FastAPI(title="SaveReel", docs_url=None, redoc_url=None)
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_cache: dict[str, tuple[float, dict]] = {}
_hits: dict[str, deque] = defaultdict(deque)


# ---------- helpers ----------
def client_ip(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (req.client.host if req.client else "?")


def rate_limit(req: Request) -> None:
    now, q = time.time(), _hits[client_ip(req)]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(429, "Too many requests. Please wait a few minutes and try again.")
    q.append(now)


def normalise_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise HTTPException(400, "Paste a Facebook video or reel link.")
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    host = (urlparse(raw).hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_PAGE_HOSTS):
        raise HTTPException(400, "That doesn't look like a Facebook link.")
    return raw


BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _json_str(raw: str) -> str:
    return json.loads('"' + raw + '"')


def _fallback_extract(url: str) -> dict:
    """Read the MP4 links Facebook embeds in the page itself (what its own player uses)."""
    with httpx.Client(headers=BROWSER_HEADERS, follow_redirects=True, timeout=20) as c:
        html = c.get(url).text
    found = {}
    for key, label, height in (("browser_native_hd_url", "HD", 720),
                               ("playable_url_quality_hd", "HD", 720),
                               ("browser_native_sd_url", "SD", 360),
                               ("playable_url", "SD", 360)):
        m = re.search(r'"%s":"(.*?)"' % key, html)
        if m and label not in found:
            u = _json_str(m.group(1))
            if (urlparse(u).hostname or "").lower().endswith(ALLOWED_CDN_SUFFIX):
                found[label] = {"id": label.lower(), "label": label, "height": height,
                                "size": None, "_url": u}
    if not found:
        login_wall = 'id="login_form"' in html or "/login/" in html[:20000]
        raise ValueError("fallback: " + ("login wall" if login_wall else "no video urls in page")
                         + f" (html {len(html)} bytes)")

    def meta(prop):
        m = re.search(r'<meta[^>]+property="%s"[^>]+content="([^"]*)"' % prop, html)
        return unescape(m.group(1)) if m else None

    dur = re.search(r'"playable_duration_in_ms":(\d+)', html)
    return {
        "title": meta("og:title") or "Facebook video",
        "uploader": None,
        "thumbnail": meta("og:image"),
        "duration": int(dur.group(1)) / 1000 if dur else None,
        "formats": sorted(found.values(), key=lambda f: f["height"], reverse=True),
    }


def _extract(url: str) -> dict:
    try:
        return _ytdlp_extract(url)
    except Exception as e:
        log.warning("yt-dlp failed for %s: %s", url, e)
        try:
            return _fallback_extract(url)
        except Exception as e2:
            log.warning("fallback failed for %s: %s", url, e2)
            raise RuntimeError(f"yt-dlp: {e} | {e2}") from e2


def _ytdlp_extract(url: str) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "http_headers": {"User-Agent": BROWSER_HEADERS["User-Agent"],
                             "Accept-Language": BROWSER_HEADERS["Accept-Language"]}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist" and info.get("entries"):
        info = next(e for e in info["entries"] if e)

    # Keep only single-file MP4s that already contain both video and audio,
    # so no server-side merging (ffmpeg) is needed.
    formats = []
    for f in info.get("formats") or []:
        u = f.get("url") or ""
        host = (urlparse(u).hostname or "").lower()
        if not host.endswith(ALLOWED_CDN_SUFFIX):
            continue
        if f.get("vcodec") == "none" or f.get("acodec") == "none":
            continue
        if f.get("protocol") not in (None, "https", "http"):
            continue
        h = f.get("height")
        label = f"{h}p" if h else (f.get("format_note") or f.get("format_id", "")).upper()
        formats.append({
            "id": f["format_id"],
            "label": label,
            "height": h or 0,
            "size": f.get("filesize") or f.get("filesize_approx"),
            "_url": u,
        })
    formats.sort(key=lambda f: f["height"], reverse=True)
    if not formats:
        raise ValueError("no downloadable formats")
    return {
        "title": info.get("title") or "Facebook video",
        "uploader": info.get("uploader"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "formats": formats,
    }


async def extract(url: str) -> dict:
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        data = await asyncio.to_thread(_extract, url)
    except Exception as e:  # yt-dlp raises many types
        msg = str(e).lower()
        log.error("extract failed: %s", e)
        if "login wall" in msg:
            raise HTTPException(422, "Facebook asked our server to log in, so this video can't be fetched right now. Try again in a while.")
        if "private" in msg or "not available" in msg:
            raise HTTPException(422, "This video is private or unavailable, so it can't be downloaded.")
        raise HTTPException(422, "Couldn't read that video. Check the link is a public Facebook video or reel.")
    _cache[url] = (time.time(), data)
    if len(_cache) > 500:  # simple size cap
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
            _cache.pop(k, None)
    return data


def safe_filename(title: str) -> str:
    name = re.sub(r"[^\w\- ]+", "", title)[:80].strip() or "facebook-video"
    return name.replace(" ", "_") + ".mp4"


# ---------- routes ----------
class InfoIn(BaseModel):
    url: str


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
async def health():
    return {"ok": True}


@app.post("/api/info")
async def info(body: InfoIn, request: Request):
    rate_limit(request)
    url = normalise_url(body.url)
    data = await extract(url)
    public = {k: v for k, v in data.items() if k != "formats"}
    public["formats"] = [{k: v for k, v in f.items() if not k.startswith("_")} for f in data["formats"]]
    public["url"] = url
    return public


@app.get("/api/download")
async def download(url: str, format_id: str, request: Request):
    rate_limit(request)
    url = normalise_url(url)
    data = await extract(url)
    fmt = next((f for f in data["formats"] if f["id"] == format_id), None)
    if not fmt:
        raise HTTPException(404, "That quality isn't available any more. Reload and try again.")

    # Only ever proxy URLs that yt-dlp returned and that live on Facebook's CDN (no SSRF).
    cdn_url = fmt["_url"]
    if not (urlparse(cdn_url).hostname or "").lower().endswith(ALLOWED_CDN_SUFFIX):
        raise HTTPException(400, "Unexpected video source.")

    client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=120), follow_redirects=True)
    upstream = await client.send(client.build_request("GET", cdn_url), stream=True)
    if upstream.status_code != 200:
        await upstream.aclose(); await client.aclose()
        _cache.pop(url, None)  # signed link probably expired
        raise HTTPException(502, "Facebook refused the download. Please try again.")

    async def body():
        try:
            async for chunk in upstream.aiter_bytes(256 * 1024):
                yield chunk
        finally:
            await upstream.aclose(); await client.aclose()

    headers = {"Content-Disposition": f'attachment; filename="{safe_filename(data["title"])}"'}
    if upstream.headers.get("content-length"):
        headers["Content-Length"] = upstream.headers["content-length"]
    return StreamingResponse(body(), media_type="video/mp4", headers=headers)


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
