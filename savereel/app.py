"""SaveReel — paste a public Facebook video/reel link, get a downloadable MP4.

How a link is resolved (first one that works wins):
  1. yt-dlp            — fast, no browser needed, but its Facebook support breaks often.
  2. headless Chrome   — opens the page like a real visitor, lets Facebook's own player
                          load the video, and reads the MP4 links the player receives.
The server never stores videos: it streams the MP4 straight through to the visitor.
"""
import asyncio
import json
import logging
import re
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
BROWSER_TIMEOUT = 25         # seconds to wait for Facebook's player to load the video
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

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


def is_cdn(u: str) -> bool:
    return (urlparse(u).hostname or "").lower().endswith(ALLOWED_CDN_SUFFIX)


# ---------- method 1: yt-dlp ----------
def _ytdlp_extract(url: str) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "http_headers": {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist" and info.get("entries"):
        info = next(e for e in info["entries"] if e)

    # Keep single-file MP4s that already contain video+audio (no ffmpeg merging needed).
    formats = []
    for f in info.get("formats") or []:
        u = f.get("url") or ""
        if not is_cdn(u) or f.get("vcodec") == "none" or f.get("acodec") == "none":
            continue
        if f.get("protocol") not in (None, "https", "http"):
            continue
        h = f.get("height")
        formats.append({"id": f["format_id"],
                        "label": f"{h}p" if h else (f.get("format_note") or f["format_id"]).upper(),
                        "height": h or 0,
                        "size": f.get("filesize") or f.get("filesize_approx"),
                        "_url": u})
    formats.sort(key=lambda f: f["height"], reverse=True)
    if not formats:
        raise ValueError("no downloadable formats")
    return {"title": info.get("title") or "Facebook video", "uploader": info.get("uploader"),
            "thumbnail": info.get("thumbnail"), "duration": info.get("duration"),
            "formats": formats}


# ---------- method 2: headless Chrome ----------
_pw = None
_browser = None
_browser_lock = asyncio.Lock()
_browser_slots = asyncio.Semaphore(1)   # one page at a time keeps memory low on small servers

# Keys Facebook's player data uses for progressive (video+audio) MP4 links.
VIDEO_KEYS = (("browser_native_hd_url", "HD", 720), ("playable_url_quality_hd", "HD", 720),
              ("browser_native_sd_url", "SD", 360), ("playable_url", "SD", 360))


async def get_browser():
    global _pw, _browser
    async with _browser_lock:
        if _browser is None or not _browser.is_connected():
            from playwright.async_api import async_playwright
            if _pw is None:
                _pw = await async_playwright().start()
            _browser = await _pw.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage", "--disable-gpu", "--no-sandbox",
                      "--disable-extensions", "--mute-audio"])
            log.info("headless browser started")
        return _browser


def parse_player_data(text: str) -> dict:
    """Pull the MP4 links out of page HTML / GraphQL JSON (escaped or not)."""
    found = {}
    for key, label, height in VIDEO_KEYS:
        if label in found:
            continue
        # Works whether the JSON is plain or nested/escaped one or more levels deep.
        m = re.search(r'%s\\*"\s*:\s*\\*"(https?:.*?)\\*"' % key, text)
        if not m:
            continue
        u = m.group(1)
        for _ in range(4):  # peel off escaping levels
            u = u.replace("\\\\", "\\").replace("\\/", "/")
        u = re.sub(r"\\u([0-9a-fA-F]{4})", lambda x: chr(int(x.group(1), 16)), u)
        u = u.replace("&amp;", "&")
        if u.startswith("http") and is_cdn(u):
            found[label] = {"id": label.lower(), "label": label, "height": height,
                            "size": None, "_url": u}
    return found


async def browser_extract(url: str) -> dict:
    browser = await get_browser()
    async with _browser_slots:
        ctx = await browser.new_context(user_agent=USER_AGENT, locale="en-US",
                                        viewport={"width": 1280, "height": 800})
        captured: list[str] = []
        try:
            page = await ctx.new_page()

            # Save bandwidth/memory: skip images, fonts and the actual video stream.
            async def block(route):
                if route.request.resource_type in ("image", "media", "font"):
                    await route.abort()
                else:
                    await route.continue_()
            await page.route("**/*", block)

            # Facebook's player fetches the video links via /api/graphql/ after load.
            async def on_response(resp):
                if "/api/graphql" in resp.url:
                    try:
                        body = await resp.text()
                        if "browser_native" in body or "playable_url" in body:
                            captured.append(body)
                    except Exception:
                        pass
            page.on("response", on_response)

            await page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT * 1000)

            found, deadline = {}, time.time() + BROWSER_TIMEOUT
            while time.time() < deadline:
                html = await page.content()
                found = parse_player_data(html + "\n".join(captured))
                if "HD" in found or ("SD" in found and time.time() > deadline - BROWSER_TIMEOUT / 2):
                    break
                await asyncio.sleep(1)

            if not found:
                html = await page.content()
                if 'id="login_form"' in html and "browser_native" not in html:
                    raise ValueError("browser: login wall")
                raise ValueError("browser: player never loaded a video link")

            meta = await page.evaluate("""() => {
                const m = p => document.querySelector(`meta[property="${p}"]`)?.content || null;
                const v = document.querySelector('video');
                return {title: m('og:title') || document.title, image: m('og:image'),
                        duration: v && isFinite(v.duration) ? v.duration : null};
            }""")
            dur = re.search(r'"playable_duration_in_ms":(\d+)', await page.content())
            return {"title": (meta.get("title") or "Facebook video").strip(),
                    "uploader": None,
                    "thumbnail": meta.get("image"),
                    "duration": int(dur.group(1)) / 1000 if dur else meta.get("duration"),
                    "formats": sorted(found.values(), key=lambda f: f["height"], reverse=True)}
        finally:
            await ctx.close()


@app.on_event("shutdown")
async def shutdown():
    if _browser:
        await _browser.close()
    if _pw:
        await _pw.stop()


# ---------- combined ----------
async def extract(url: str) -> dict:
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    errors = []
    data = None
    try:
        data = await asyncio.to_thread(_ytdlp_extract, url)
        log.info("yt-dlp ok for %s", url)
    except Exception as e:
        errors.append(f"yt-dlp: {e}")
        log.warning("yt-dlp failed for %s: %s", url, e)
        try:
            data = await asyncio.wait_for(browser_extract(url), BROWSER_TIMEOUT + 20)
            log.info("browser ok for %s", url)
        except Exception as e2:
            errors.append(f"browser: {e2!r}")
            log.error("extract failed for %s: %s", url, " | ".join(errors))

    if data is None:
        msg = " ".join(errors).lower()
        if "login wall" in msg:
            raise HTTPException(422, "Facebook asked our server to log in, so this video can't be fetched right now. Try again in a while.")
        if "private" in msg or "not available" in msg:
            raise HTTPException(422, "This video is private or unavailable, so it can't be downloaded.")
        raise HTTPException(422, "Couldn't read that video. Check the link is a public Facebook video or reel.")

    _cache[url] = (time.time(), data)
    if len(_cache) > 500:
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

    # Only ever proxy URLs we extracted ourselves that live on Facebook's CDN (no SSRF).
    cdn_url = fmt["_url"]
    if not is_cdn(cdn_url):
        raise HTTPException(400, "Unexpected video source.")

    client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=120), follow_redirects=True,
                               headers={"User-Agent": USER_AGENT, "Referer": "https://www.facebook.com/"})
    upstream = await client.send(client.build_request("GET", cdn_url), stream=True)
    if upstream.status_code != 200:
        await upstream.aclose(); await client.aclose()
        _cache.pop(url, None)  # signed link probably expired
        log.warning("CDN returned %s for %s", upstream.status_code, url)
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
