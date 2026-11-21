import os
import re
import json
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


def extract_instagram_embed(url: str, proxy: str | None = None) -> dict:
    """Extracts all carousel slides (images & videos) from Instagram's public embed endpoint."""
    match = re.search(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url)
    if not match:
        raise ValueError("Not an Instagram post URL")

    shortcode = match.group(1)
    embed_url = f"https://instagram.com{shortcode}/embed/captioned/"
    proxies = {"http": proxy, "https": proxy} if proxy else None

    resp = cffi_requests.get(embed_url, impersonate="chrome", proxies=proxies, timeout=12)
    if resp.status_code != 200:
        raise ValueError(f"Embed HTTP {resp.status_code}")

    html = resp.text
    items = []

    # 1. Parse embedded JSON object containing edge_sidecar_to_children
    json_match = re.search(r'"gql_data"\s*:\s*(\{.*?\})\s*,\s*"', html)
    if not json_match:
        json_match = re.search(r'window\.__additionalDataLoaded\([^,]+,\s*(\{.*?\})\);', html)

    if json_match:
        try:
            data = json.loads(json_match.group(1))
            media = (
                data.get("shortcode_media")
                or data.get("graphql", {}).get("shortcode_media")
                or {}
            )
            edges = (
                media.get("edge_sidecar_to_children", {}).get("edges", [])
            )
            nodes = [e["node"] for e in edges if "node" in e] or ([media] if media else [])

            for idx, node in enumerate(nodes, start=1):
                is_video = node.get("is_video", False)
                media_url = node.get("video_url") if is_video else node.get("display_url")
                thumb = node.get("display_url")
                if media_url and is_allowed_cdn(media_url):
                    items.append(
                        {
                            "index": idx,
                            "media_type": "video" if is_video else "image",
                            "media_url": media_url,
                            "thumbnail": thumb,
                            "ext": "mp4" if is_video else "jpg",
                        }
                    )
        except Exception:
            pass

    # 2. Fallback regex across escaped/unescaped display_url & video_url in embed HTML
    if not items:
        soup = BeautifulSoup(html, "html.parser")
        seen = set()
        for img in soup.select("img.EmbeddedMediaImage, img[src*='cdninstagram.com'], img[src*='fbcdn.net']"):
            src = img.get("src", "")
            # Filter out tiny profile avatars (usually 150x150 or s150x150)
            if src and is_allowed_cdn(src) and "s150x150" not in src and src not in seen:
                seen.add(src)
                items.append(
                    {
                        "index": len(items) + 1,
                        "media_type": "image",
                        "media_url": src,
                        "thumbnail": src,
                        "ext": "jpg",
                    }
                )

    if not items:
        raise ValueError("No carousel items found in embed")

    soup = BeautifulSoup(html, "html.parser")
    caption_el = soup.select_one(".Caption")
    user_el = soup.select_one(".UsernameText")

    return {
        "title": "Instagram Post",
        "description": caption_el.get_text(" ", strip=True) if caption_el else "",
        "uploader": user_el.get_text(strip=True) if user_el else "Instagram",
        "items": items,
    }


def extract_og_fallback(url: str, proxy: str | None = None) -> dict:
    proxies = {"http": proxy, "https": proxy} if proxy else None
    resp = cffi_requests.get(url, impersonate="chrome", proxies=proxies, timeout=12)
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}")

    soup = BeautifulSoup(resp.text, "html.parser")

    def get_meta(prop):
        tag = soup.find("meta", property=prop)
        return tag["content"] if tag and tag.get("content") else None

    title = get_meta("og:title")
    description = get_meta("og:description")
    og_image = get_meta("og:image")

    if not any([title, og_image, description]):
        raise ValueError("Login wall hit or no public metadata found")

    items = []
    if og_image:
        items.append(
            {
                "index": 1,
                "media_type": "image",
                "media_url": og_image,
                "thumbnail": og_image,
                "ext": "jpg",
            }
        )

    return {
        "title": title or "Public Post",
        "description": description or "",
        "uploader": urlparse(url).netloc,
        "items": items,
    }


def attempt_extraction(url: str, proxy: str | None = None) -> dict:
    # 1. Try Instagram embed endpoint first for Instagram /p/ posts (captures multi-slide carousels)
    if "instagram.com" in urlparse(url).netloc.lower():
        try:
            return extract_instagram_embed(url, proxy=proxy)
        except Exception:
            pass

    # 2. Try yt-dlp (great for Reels, Facebook videos, and single video posts)
    try:
        return extract_with_ytdlp(url, proxy=proxy)
    except Exception:
        pass

    # 3. Fallback to OpenGraph metadata
    return extract_og_fallback(url, proxy=proxy)


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

    try:
        target_url = resolve_redirects(raw_url, proxy=None)
        result = attempt_extraction(target_url, proxy=None)
