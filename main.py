from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl
import yt_dlp
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse

app = FastAPI()

class UrlRequest(BaseModel):
    url: HttpUrl

def is_supported_domain(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    allowed = ("instagram.com", "://instagram.com", "facebook.com", "://facebook.com", "fb.watch")
    return host in allowed

def extract_with_ytdlp(url: str) -> dict:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return {
            "source": info.get("extractor_key"),
            "type": "video_or_post",
            "title": info.get("title"),
            "description": info.get("description"),
            "uploader": info.get("uploader"),
            "thumbnail": info.get("thumbnail"),
            "media_url": info.get("url"),
        }

def extract_og_metadata(url: str) -> dict:
    resp = cffi_requests.get(url, impersonate="chrome", timeout=10)
    if resp.status_code != 200:
        raise ValueError(f"Blocked or unavailable (HTTP {resp.status_code})")

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
        "source": urlparse(url).netloc,
        "type": "metadata_preview",
        "title": title,
        "description": description,
        "thumbnail": image,
    }

@app.post("/fetch")
def fetch_public_content(payload: UrlRequest):
    url = str(payload.url)
    if not is_supported_domain(url):
        raise HTTPException(status_code=400, detail="Only Instagram and Facebook URLs are supported.")

    # 1. Try yt-dlp first (best for Reels, videos, and many public posts)
    try:
        return extract_with_ytdlp(url)
    except Exception:
        pass

    # 2. Fallback to OpenGraph tag scraping (best for profiles or standard image/text posts)
    try:
        return extract_og_metadata(url)
    except Exception as e:
        raise HTTPException(
            status_code=422,
            detail=f"Could not extract public data without login. Meta may have gated this URL. ({e})"
        )
