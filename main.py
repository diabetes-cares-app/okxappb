from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, HttpUrl
from urllib.parse import urlparse, quote
from cachetools import TTLCache
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import yt_dlp
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup
import httpx
import re

# Rate limiter: keyed by client IP
limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Replace with your frontend domain in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cache up to 500 URLs for 30 minutes (1800s) — Meta CDN tokens expire after a few hours
response_cache = TTLCache(maxsize=500, ttl=1800)

ALLOWED_SOURCE_DOMAINS = (
    "instagram.com", "://instagram.com",
    "facebook.com", "://facebook.com", "fb.watch"
)

ALLOWED_CDN_SUFFIXES = (
    ".cdninstagram.com", ".fbcdn.net", ".fbsbx.com"
)

class UrlRequest(BaseModel):
    url: HttpUrl

def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    # Strip tracking query params (e.g. ?igsh=...) so cache hits work reliably
    return f"{parsed.scheme}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"

def is_supported_domain(url: str) -> bool:
    return urlparse(url).netloc.lower() in ALLOWED_SOURCE_DOMAINS

def is_allowed_cdn(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host.endswith(suffix) for suffix in ALLOWED_CDN_SUFFIXES)

def extract_with_ytdlp(url: str) -> dict:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "best[ext=mp4]/best",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        items = []

        # yt-dlp returns 'entries' for carousels / multi-item posts
        entries = info.get("entries") or [info]
        for idx, entry in enumerate(entries, start=1):
            if not entry:
                continue
            media_url = entry.get("url")
            thumb = entry.get("thumbnail")
            ext = entry.get("ext") or "mp4"
            is_video = ext in ("mp4", "webm", "mov") or bool(entry.get("vcodec") and entry.get("vcodec") != "none")

            if media_url:
                items.append({
                    "index": idx,
                    "media_type": "video" if is_video else "image",
                    "media_url": media_url,
                    "thumbnail": thumb,
                    "ext": ext if is_video else "jpg",
                })

        if not items:
            raise ValueError("No media items extracted")

        return {
            "title": info.get("title") or "Social Media Post",
            "description": info.get("description") or "",
            "uploader": info.get("uploader") or info.get("channel") or "Public Account",
            "items": items,
        }

def extract_og_and_carousel_fallback(url: str) -> dict:
    resp = cffi_requests.get(url, impersonate="chrome", timeout=10)
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}")

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    def get_meta(prop):
        tag = soup.find("meta", property=prop)
        return tag["content"] if tag and tag.get("content") else None

    title = get_meta("og:title")
    description = get_meta("og:description")
    og_image = get_meta("og:image")

    # Attempt to find display_url entries embedded in public Instagram JSON blobs
    raw_urls = re.findall(r'"display_url"\s*:\s*"(https:[^"]+)"', html)
    clean_urls = []
    seen = set()
    for u in raw_urls:
        decoded = u.replace("\\u0026", "&").replace("\\/", "/")
        if decoded not in seen and is_allowed_cdn(decoded):
            seen.add(decoded)
            clean_urls.append(decoded)

    if not clean_urls and og_image:
        clean_urls = [og_image]

    if not clean_urls and not any([title, description]):
        raise ValueError("Login wall hit or no public metadata found")

    items = [
        {
            "index": idx,
            "media_type": "image",
            "media_url": img_url,
            "thumbnail": img_url,
            "ext": "jpg",
        }
        for idx, img_url in enumerate(clean_urls, start=1)
    ]

    return {
        "title": title or "Public Post",
        "description": description or "",
        "uploader": urlparse(url).netloc,
        "items": items,
    }

@app.post("/fetch")
@limiter.limit("15/minute")  # Max 15 lookups per minute per IP
def fetch_public_content(request: Request, payload: UrlRequest):
    raw_url = str(payload.url)
    if not is_supported_domain(raw_url):
        raise HTTPException(status_code=400, detail="Only Instagram and Facebook URLs are supported.")

    cache_key = normalize_url(raw_url)
    if cache_key in response_cache:
        return {**response_cache[cache_key], "cached": True}

    result = None
    try:
        result = extract_with_ytdlp(raw_url)
    except Exception:
        pass

    if not result:
        try:
            result = extract_og_and_carousel_fallback(raw_url)
        except Exception as e:
            raise HTTPException(
                status_code=422,
                detail=f"Could not extract public data without login. ({e})"
            )

    response_cache[cache_key] = result
    return {**result, "cached": False}

@app.get("/proxy")
@limiter.limit("60/minute")  # Higher limit since carousels load multiple slides at once
async def proxy_media(
    request: Request,
    media_url: str = Query(...),
    download: bool = Query(False),
    filename: str = Query("media.mp4")
):
    if not is_allowed_cdn(media_url):
        raise HTTPException(status_code=400, detail="Invalid media host.")

    client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)
    req = client.build_request("GET", media_url, headers={"User-Agent": "Mozilla/5.0"})
    resp = await client.send(req, stream=True)

    if resp.status_code >= 400:
        await resp.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail="Failed to fetch media from CDN.")

    headers = {}
    if download:
        safe_name = quote(filename)
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{safe_name}"

    async def stream_generator():
        try:
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_generator(),
        media_type=resp.headers.get("content-type", "application/octet-stream"),
        headers=headers,
    )
