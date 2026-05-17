"""
Quicklify YouTube API — lightweight yt-dlp wrapper.

Endpoints
---------
POST /download
  body: {"url": "...", "quality": "1080", "mode": "auto|audio|mute", "audioFormat": "mp3"}
  returns: {"status": "ok", "url": "...", "filename": "...", "quality": "..."} or
           {"status": "error", "message": "..."}

GET  /health
  returns: {"status": "ok", "version": "<yt-dlp version>", "cookies": true/false}

Setup
-----
YouTube blocks requests from server IPs. To fix this, set the YT_COOKIES
environment variable with the contents of a Netscape-format cookies.txt file
exported from your browser.

How to export cookies:
  1. Install the "Get cookies.txt LOCALLY" browser extension
  2. Go to youtube.com (make sure you're logged in)
  3. Click the extension → export cookies for youtube.com
  4. Copy the entire file contents
  5. In Railway: Settings → Variables → add YT_COOKIES → paste the contents
"""

import os
import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("yt-api")

# ── Config ────────────────────────────────────────────────────────────
API_KEY = os.environ.get("API_KEY", "")
YT_DLP_VERSION = yt_dlp.version.__version__
COOKIES_PATH: str | None = None


def _setup_cookies() -> str | None:
    """Write YT_COOKIES env var to a temp file for yt-dlp."""
    raw = os.environ.get("YT_COOKIES", "").strip()
    if not raw:
        return None
    path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
    with open(path, "w") as f:
        f.write(raw)
    logger.info(f"Cookies written to {path} ({len(raw)} chars)")
    return path


# ── Lifespan ──────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global COOKIES_PATH
    COOKIES_PATH = _setup_cookies()
    if COOKIES_PATH:
        logger.info(f"YouTube API starting — yt-dlp {YT_DLP_VERSION} — cookies loaded")
    else:
        logger.warning(
            f"YouTube API starting — yt-dlp {YT_DLP_VERSION} — "
            "NO COOKIES SET (set YT_COOKIES env var to avoid bot detection)"
        )
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
    status: str
    url: str | None = None
    audioUrl: str | None = None       # set when video+audio are separate
    needsMerge: bool = False           # true → app must merge video + audio
    filename: str | None = None
    quality: str | None = None
    filesize: int | None = None
    message: str | None = None


# ── Auth middleware (optional) ────────────────────────────────────────
@app.middleware("http")
async def check_api_key(request: Request, call_next):
    if API_KEY and request.url.path not in ("/health", "/docs", "/openapi.json"):
        key = request.headers.get("x-api-key", "")
        if key != API_KEY:
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
        format_str = "bestaudio[ext=m4a]/bestaudio"
    elif req.mode == "mute":
        format_str = (
            f"bestvideo[height<={target_height}][ext=mp4]/"
            f"bestvideo[height<={target_height}]/"
            "bestvideo[ext=mp4]/bestvideo"
        )
    else:
        format_str = (
            f"bestvideo[height<={target_height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={target_height}]+bestaudio/"
            f"best[height<={target_height}][ext=mp4]/"
            f"best[height<={target_height}]/best"
        )

    ydl_opts: dict = {
        "format": format_str,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": False,
    }

    # Pass cookies if available — critical to bypass bot detection
    if COOKIES_PATH and os.path.exists(COOKIES_PATH):
        ydl_opts["cookiefile"] = COOKIES_PATH

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(req.url, download=False)
        if info is None:
            return StreamInfo(status="error", message="Could not extract video info")

        requested = info.get("requested_formats")
        title = info.get("title", "video")

        logger.info(f"requested_formats count: {len(requested) if requested else 0}")

        # yt-dlp returns requested_formats with [video, audio] when they're separate
        if requested and len(requested) >= 2 and not is_audio:
            video_fmt = requested[0]
            audio_fmt = requested[1]
            video_url = video_fmt.get("url")
            audio_url = audio_fmt.get("url")

            if video_url and audio_url:
                height = video_fmt.get("height") or info.get("height") or target_height
                v_size = video_fmt.get("filesize") or video_fmt.get("filesize_approx") or 0
                a_size = audio_fmt.get("filesize") or audio_fmt.get("filesize_approx") or 0
                filename = f"{title} ({height}p).mp4"

                return StreamInfo(
                    status="ok",
                    url=video_url,
                    audioUrl=audio_url,
                    needsMerge=True,
                    filename=filename,
                    quality=f"{height}p",
                    filesize=(v_size + a_size) if v_size and a_size else None,
                )

        # Single format (muxed, audio-only, or fallback)
        fmt = None
        if requested:
            fmt = requested[-1] if is_audio else requested[0]
        else:
            fmt = info

        stream_url = fmt.get("url") or info.get("url")
        if not stream_url:
            return StreamInfo(status="error", message="No download URL found")

        # Check if this is a video-only format (no audio) — if so, we need
        # to separately fetch audio and tell the app to merge them.
        acodec = fmt.get("acodec") or "none"
        vcodec = fmt.get("vcodec") or "none"
        is_video_only = vcodec != "none" and acodec == "none"

        if is_video_only and not is_audio and req.mode != "mute":
            # Re-extract to get best audio URL
            logger.info(f"Format is video-only (itag={fmt.get('format_id')}), fetching audio separately")
            audio_opts = dict(ydl_opts)
            audio_opts["format"] = "bestaudio[ext=m4a]/bestaudio"
            with yt_dlp.YoutubeDL(audio_opts) as ydl_audio:
                audio_info = ydl_audio.extract_info(req.url, download=False)
                audio_url = None
                if audio_info:
                    audio_requested = audio_info.get("requested_formats")
                    if audio_requested:
                        audio_url = audio_requested[0].get("url")
                    else:
                        audio_url = audio_info.get("url")

                if audio_url:
                    height = fmt.get("height") or info.get("height") or target_height
                    v_size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
                    filename = f"{title} ({height}p).mp4"
                    return StreamInfo(
                        status="ok",
                        url=stream_url,
                        audioUrl=audio_url,
                        needsMerge=True,
                        filename=filename,
                        quality=f"{height}p",
                        filesize=v_size,
                    )
                else:
                    logger.warning("Could not find audio stream for merge")

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
    return {
        "status": "ok",
        "version": YT_DLP_VERSION,
        "cookies": COOKIES_PATH is not None and os.path.exists(COOKIES_PATH),
    }
