from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, HttpUrl
from urllib.parse import urlparse, quote
import yt_dlp
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup
import httpx

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Lock this down to your domain in production
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_SOURCE_DOMAINS = (
    "instagram.com", "://instagram.com",
    "facebook.com", "://facebook.com", "fb.watch"
)

ALLOWED_CDN_SUFFIXES = (
    ".cdninstagram.com", ".fbcdn.net", ".fbsbx.com"
)

class UrlRequest(BaseModel):
    url: HttpUrl

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
        return {
            "type": "video",
            "title": info.get("title") or "Social Media Video",
            "description": info.get("description") or "",
            "uploader": info.get("uploader") or info.get("channel") or "Public Account",
            "thumbnail": info.get("thumbnail"),
            "media_url": info.get("url"),
            "ext": info.get("ext") or "mp4",
        }

def extract_og_metadata(url: str) -> dict:
    resp = cffi_requests.get(url, impersonate="chrome", timeout=10)
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}")

    soup = BeautifulSoup(resp.text, "html.parser")
    def get_meta(prop):
        tag = soup.find("meta", property=prop)
        return tag["content"] if tag and tag.get("content") else None

    title = get_meta("og:title")
    image = get_meta("og:image")
    description = get_meta("og:description")

    if not any():
        raise ValueError("Login wall hit or no public metadata found")

    return {
        "type": "image_or_post",
        "title": title or "Public Post",
        "description": description or "",
        "uploader": urlparse(url).netloc,
        "thumbnail": image,
        "media_url": image,
        "ext": "jpg",
    }

@app.post("/fetch")
def fetch_public_content(payload: UrlRequest):
    url = str(payload.url)
    if not is_supported_domain(url):
        raise HTTPException(status_code=400, detail="Only Instagram and Facebook URLs are supported.")

    try:
        return extract_with_ytdlp(url)
    except Exception:
        pass

    try:
        return extract_og_metadata(url)
    except Exception as e:
        raise HTTPException(
            status_code=422,
            detail=f"Could not extract public data without login. ({e})"
        )

@app.get("/proxy")
async def proxy_media(
    media_url: str = Query(...),
    download: bool = Query(False),
    filename: str = Query("media.mp4")
):
    # Prevent open-proxy abuse by restricting to Meta CDNs
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
