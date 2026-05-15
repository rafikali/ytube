"""
Quicklify YouTube API — lightweight yt-dlp wrapper.

Endpoints
---------
POST /download
  body: {"url": "...", "quality": "1080", "mode": "auto|audio|mute", "audioFormat": "mp3"}
  returns: {"status": "ok", "url": "...", "filename": "...", "quality": "..."} or
           {"status": "error", "message": "..."}

GET  /health
  returns: {"status": "ok", "version": "<yt-dlp version>"}
"""

import os
import logging
import subprocess
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("yt-api")

# ── Config ────────────────────────────────────────────────────────────
API_KEY = os.environ.get("API_KEY", "")  # optional: protect with a key
YT_DLP_VERSION = yt_dlp.version.__version__


# ── Lifespan ──────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"YouTube API starting — yt-dlp {YT_DLP_VERSION}")
    yield
    logger.info("Shutting down")


app = FastAPI(title="Quicklify YouTube API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Models ────────────────────────────────────────────────────────────
class DownloadRequest(BaseModel):
    url: str
    quality: str = "1080"
    mode: str = "auto"          # auto | audio | mute
    audioFormat: str = "mp3"    # mp3 | ogg | wav | opus


class StreamInfo(BaseModel):
    status: str             # "ok" or "error"
    url: str | None = None
    filename: str | None = None
    quality: str | None = None
    filesize: int | None = None
    message: str | None = None


# ── Auth middleware (optional) ────────────────────────────────────────
from fastapi import Request

@app.middleware("http")
async def check_api_key(request: Request, call_next):
    if API_KEY and request.url.path not in ("/health", "/docs", "/openapi.json"):
        key = request.headers.get("x-api-key", "")
        if key != API_KEY:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=401,
                content={"status": "error", "message": "Invalid API key"},
            )
    return await call_next(request)


# ── Helpers ───────────────────────────────────────────────────────────
def _extract(req: DownloadRequest) -> StreamInfo:
    """Use yt-dlp to extract the best matching direct stream URL."""

    target_height = int(req.quality) if req.quality.isdigit() else 1080
    is_audio = req.mode == "audio"

    if is_audio:
        # Audio-only: best audio in the requested format
        format_str = "bestaudio[ext=m4a]/bestaudio"
    elif req.mode == "mute":
        # Video-only (no audio)
        format_str = (
            f"bestvideo[height<={target_height}][ext=mp4]/"
            f"bestvideo[height<={target_height}]/"
            "bestvideo[ext=mp4]/bestvideo"
        )
    else:
        # Combined video + audio — prefer mp4 container
        format_str = (
            f"bestvideo[height<={target_height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={target_height}]+bestaudio/"
            f"best[height<={target_height}][ext=mp4]/"
            f"best[height<={target_height}]/best"
        )

    ydl_opts = {
        "format": format_str,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        # Don't post-process — we just want the URL
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": False,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(req.url, download=False)
        if info is None:
            return StreamInfo(status="error", message="Could not extract video info")

        # yt-dlp may merge formats; pick the requested one
        requested = info.get("requested_formats")
        fmt = None
        if requested:
            # For combined: first is video, second is audio
            if is_audio:
                fmt = requested[-1]  # audio track
            else:
                fmt = requested[0]   # video track (or merged)
        else:
            fmt = info  # single format selected

        stream_url = fmt.get("url") or info.get("url")
        if not stream_url:
            return StreamInfo(status="error", message="No download URL found")

        title = info.get("title", "video")
        ext = fmt.get("ext", "mp4") if not is_audio else (
            "m4a" if "m4a" in (fmt.get("ext", "") or "") else req.audioFormat
        )
        height = fmt.get("height") or info.get("height") or target_height
        filesize = fmt.get("filesize") or fmt.get("filesize_approx")

        filename = f"{title} ({height}p).{ext}" if not is_audio else f"{title}.{ext}"

        return StreamInfo(
            status="ok",
            url=stream_url,
            filename=filename,
            quality=f"{height}p" if not is_audio else fmt.get("abr", "audio"),
            filesize=filesize,
        )


# ── Endpoints ─────────────────────────────────────────────────────────
@app.post("/download", response_model=StreamInfo)
async def download(req: DownloadRequest):
    logger.info(f"Request: url={req.url} quality={req.quality} mode={req.mode}")
    try:
        result = _extract(req)
        if result.status == "error":
            logger.warning(f"Extraction failed: {result.message}")
        else:
            logger.info(f"Success: {result.filename} — {result.quality}")
        return result
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).split("\n")[0]
        logger.error(f"yt-dlp DownloadError: {msg}")
        return StreamInfo(status="error", message=msg)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return StreamInfo(status="error", message=str(e))


@app.get("/health")
async def health():
    return {"status": "ok", "version": YT_DLP_VERSION}
