import os
import re
from urllib.parse import urlparse, quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl
from cachetools import TTLCache
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import yt_dlp
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup
import httpx

# Optional residential proxy URL from Railway environment variables
# Format example: http://provider.com
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None


def get_real_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=get_real_ip)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cache up to 500 URLs for 30 minutes (1800s)
response_cache = TTLCache(maxsize=500, ttl=1800)

ALLOWED_EXACT_DOMAINS = (
    "instagram.com",
    "instagr.am",
    "facebook.com",
    "fb.watch",
    "fb.com",
)

ALLOWED_CDN_SUFFIXES = (
    ".cdninstagram.com",
    ".fbcdn.net",
    ".fbsbx.com",
)


class UrlRequest(BaseModel):
    url: HttpUrl


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    return f"{parsed.scheme}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def is_supported_domain(url: str) -> bool:
    host = urlparse(url.strip()).netloc.lower().split(":")[0]
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in ALLOWED_EXACT_DOMAINS
    )


def is_allowed_cdn(url: str) -> bool:
    host = urlparse(url).netloc.lower().split(":")[0]
    return any(host.endswith(suffix) for suffix in ALLOWED_CDN_SUFFIXES)


def resolve_redirects(url: str, proxy: str | None = None) -> str:
    """Follow short links like ://facebook.com... or fb.watch/... to their final URL."""
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = cffi_requests.get(
            url,
            impersonate="chrome",
            proxies=proxies,
            allow_redirects=True,
            timeout=10,
        )
        final_url = str(resp.url)
        if is_supported_domain(final_url) and "/login" not in final_url:
            return final_url
    except Exception:
        pass
    return url


def extract_with_ytdlp(url: str, proxy: str | None = None) -> dict:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "best[ext=mp4]/best",
    }
    if proxy:
        ydl_opts["proxy"] = proxy

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        items = []

        entries = info.get("entries") or [info]
        for idx, entry in enumerate(entries, start=1):
            if not entry:
                continue
            media_url = entry.get("url")
            thumb = entry.get("thumbnail")
            ext = entry.get("ext") or "mp4"
            is_video = ext in ("mp4", "webm", "mov") or bool(
                entry.get("vcodec") and entry.get("vcodec") != "none"
            )

            if media_url:
                items.append(
                    {
                        "index": idx,
                        "media_type": "video" if is_video else "image",
                        "media_url": media_url,
                        "thumbnail": thumb,
                        "ext": ext if is_video else "jpg",
                    }
                )

        if not items:
            raise ValueError("No media items extracted")

        return {
            "title": info.get("title") or "Social Media Post",
            "description": info.get("description") or "",
            "uploader": info.get("uploader") or info.get("channel") or "Public Account",
            "items": items,
        }


def extract_og_and_carousel_fallback(url: str, proxy: str | None = None) -> dict:
    proxies = {"http": proxy, "https": proxy} if proxy else None
    resp = cffi_requests.get(url, impersonate="chrome", proxies=proxies, timeout=12)
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


def attempt_extraction(url: str, proxy: str | None = None) -> dict:
    try:
        return extract_with_ytdlp(url, proxy=proxy)
    except Exception:
        return extract_og_and_carousel_fallback(url, proxy=proxy)


@app.post("/fetch")
@limiter.limit("15/minute")
def fetch_public_content(request: Request, payload: UrlRequest):
    raw_url = str(payload.url).strip()
    if not is_supported_domain(raw_url):
        raise HTTPException(
            status_code=400,
            detail=f"Only Instagram and Facebook URLs are supported. (Got: {urlparse(raw_url).netloc})",
        )

    cache_key = normalize_url(raw_url)
    if cache_key in response_cache:
        return {**response_cache[cache_key], "cached": True}

    result = None
    last_error = None

    # 1. Try direct server request first (resolving /share/ links)
    try:
        target_url = resolve_redirects(raw_url, proxy=None)
        result = attempt_extraction(target_url, proxy=None)
    except Exception as e:
        last_error = e

    # 2. Fallback to PROXY_URL if configured and direct attempt failed
    if not result and PROXY_URL:
        try:
            target_url = resolve_redirects(raw_url, proxy=PROXY_URL)
            result = attempt_extraction(target_url, proxy=PROXY_URL)
        except Exception as e:
            last_error = e

    if not result:
        raise HTTPException(
            status_code=422,
            detail=f"Could not extract public data without login. ({last_error})",
        )

    response_cache[cache_key] = result
    return {**result, "cached": False}


@app.get("/proxy")
@limiter.limit("60/minute")
async def proxy_media(
    request: Request,
    media_url: str = Query(...),
    download: bool = Query(False),
    filename: str = Query("media.mp4"),
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


@app.get("/health")
def health():
    return {"status": "ok", "proxy_configured": bool(PROXY_URL)}


os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_frontend():
    return FileResponse("static/index.html")
