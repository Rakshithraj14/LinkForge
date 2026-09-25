"""LinkForge — paste a link, get the file. Thin HTTP shell around yt-dlp."""

import html
import ipaddress
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

MAX_FILESIZE = os.getenv("MAX_FILESIZE", "500M")
TIMEOUT = int(os.getenv("TIMEOUT", "600"))
COOKIES_FILE = os.getenv("COOKIES_FILE", "")
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "10"))  # downloads per IP per hour; 0 disables
PREVIEW_LIMIT = int(os.getenv("PREVIEW_LIMIT", "30"))  # previews per IP per hour; 0 disables
PREVIEW_TIMEOUT = int(os.getenv("PREVIEW_TIMEOUT", "20"))
WINDOW = 3600

# mode -> extra yt-dlp args. The three media modes also embed whatever
# metadata/chapters/thumbnail the site provides, at no extra cost to the user.
MODES = {
    "both": ["-f", "bv*+ba/b", "--embed-metadata", "--embed-chapters"],
    "video": ["-f", "bv*/b", "--embed-metadata", "--embed-chapters"],
    "audio": ["-f", "ba/b", "-x", "--embed-metadata", "--embed-thumbnail"],
    "subtitles": ["--skip-download", "--write-subs",
                  "--sub-langs", "all", "--convert-subs", "srt"],
    "auto_subtitles": ["--skip-download", "--write-auto-subs",
                        "--sub-langs", "all", "--convert-subs", "srt"],
    "thumbnail": ["--skip-download", "--write-thumbnail"],
    "info": ["--skip-download", "--write-info-json"],
}

# Whitelists for the quality/format pickers. Anything else is a 400, so
# user input never reaches yt-dlp's format syntax unchecked.
AUDIO_QUALITIES = ("best", "320", "192", "128")  # kbps
VIDEO_FORMATS = ("mp4", "webm", "mkv")
AUDIO_FORMATS = ("mp3", "m4a", "opus", "flac")


def quality_args(mode: str, quality: str, fmt: str) -> list[str]:
    """Extra yt-dlp args for the chosen quality and container/codec."""
    if mode == "audio":
        if fmt not in AUDIO_FORMATS or quality not in AUDIO_QUALITIES:
            raise HTTPException(400, "Unknown audio format or quality.")
        return ["--audio-format", fmt, "--audio-quality", "0" if quality == "best" else f"{quality}K"]
    if mode not in ("both", "video"):
        return []
    # Video quality is a resolution from the preview (e.g. 1080, or 1152 on odd uploads).
    if not (quality == "best" or (quality.isdigit() and 0 < int(quality) <= 4320)) or fmt not in VIDEO_FORMATS:
        raise HTTPException(400, "Unknown quality or format.")
    # -S res:N prefers the tallest stream not above N; ext:fmt prefers streams
    # that fit the container. mkv is the fallback when codecs don't fit.
    sort = ([f"res:{quality}"] if quality != "best" else []) + [f"ext:{fmt}"]
    return ["-S", ",".join(sort), "--merge-output-format", f"{fmt}/mkv"]


# Shown when yt-dlp succeeds but produced no file — e.g. the video has no subtitles.
NOTHING_FOUND = {
    "subtitles": "No subtitles available for that link.",
    "auto_subtitles": "No auto-generated captions available for that link.",
    "thumbnail": "No thumbnail available for that link.",
    "info": "Could not fetch info for that link.",
}

log = logging.getLogger("linkforge")
app = FastAPI()
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
_hits: dict[str, list[float]] = defaultdict(list)


def check_url(url: str) -> None:
    """Reject anything that isn't a public http(s) address.

    Two things this stops: `file:///etc/passwd`, and `http://169.254.169.254/`
    reading the VPS metadata service through our own server.
    """
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise HTTPException(400, "Only http(s) links.")
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(p.hostname, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, ValueError):
        raise HTTPException(400, "Could not resolve that host.")
    # ponytail: TOCTOU — yt-dlp resolves again and could get a different answer.
    # Closing it means a custom resolver; not worth it for this threat model.
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise HTTPException(400, "That address is not allowed.")


def check_rate(ip: str, bucket: str = "dl", limit: int | None = None) -> None:
    if limit is None:
        limit = RATE_LIMIT
    if not limit:
        return
    key = f"{bucket}:{ip}"
    now = time.time()
    # ponytail: in-memory — resets on restart, one container only. Redis if you scale out.
    hits = [t for t in _hits[key] if now - t < WINDOW]
    if len(hits) >= limit:
        raise HTTPException(429, f"Max {limit} per hour.")
    hits.append(now)
    _hits[key] = hits
    if len(_hits) > 5000:  # drop keys whose window has fully expired
        for k in [k for k, v in _hits.items() if all(now - t >= WINDOW for t in v)]:
            del _hits[k]


def reason(stderr: str) -> str:
    """A useful sentence for the user. Never the raw stderr — it carries paths and cookies."""
    s = stderr.lower()
    if "not a bot" in s or "sign in to confirm" in s:
        return "The site blocked this server. It needs fresh cookies."
    if "429" in s or "too many requests" in s:
        return "The site is rate-limiting this server right now. Try again shortly."
    if "filesize" in s or "larger than" in s:
        return f"That file is bigger than {MAX_FILESIZE}."
    if "unsupported url" in s or "no video" in s:
        return "Nothing downloadable at that link."
    if "private" in s or "login" in s or "unavailable" in s or "removed" in s:
        return "That post is private, removed, or needs a login."
    return "Download failed."


INDEX_HTML = (Path(__file__).parent / "index.html").read_text()


@app.get("/")
def index(request: Request):
    # Share previews (WhatsApp, Slack, X...) need an absolute image URL, and we
    # only learn our public address per request. Escaped: Host is client-sent.
    origin = html.escape(str(request.base_url).rstrip("/"), quote=True)
    return HTMLResponse(INDEX_HTML.replace("{{origin}}", origin))


@app.get("/preview")
def preview(request: Request, url: str):
    check_rate(request.client.host if request.client else "unknown",
               bucket="preview", limit=PREVIEW_LIMIT)
    check_url(url)

    cmd = ["yt-dlp", "-j", "--skip-download", "--no-playlist", "--no-warnings",
           "--ignore-config"]
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        cmd += ["--cookies", COOKIES_FILE]
    cmd += ["--", url]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=PREVIEW_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Preview took too long.")
    if r.returncode != 0:
        raise HTTPException(502, reason(r.stderr))

    try:
        info = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise HTTPException(502, "Could not read that link.")

    return {
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),  # seconds, or None for images/live
        "uploader": info.get("uploader"),
        "verified": bool(info.get("channel_is_verified")),
        "view_count": info.get("view_count"),
        "upload_date": info.get("upload_date"),  # YYYYMMDD
        "site": info.get("extractor_key"),
        # Resolutions on offer, tallest first. min(w, h) so a vertical 1080x1920
        # clip reads as 1080p, matching how yt-dlp's res: sort measures it.
        "heights": sorted({min(f["width"], f["height"]) if f.get("width") else f["height"]
                           for f in info.get("formats") or []
                           if f.get("height") and f.get("vcodec") not in (None, "none")}, reverse=True),
    }


# Sync def, so FastAPI runs this in a threadpool — the blocking subprocess call
# below never stalls the event loop, and concurrent downloads just work.
@app.post("/download")
def download(request: Request, url: str = Form(...), mode: str = Form("both"),
             quality: str = Form("1080"), fmt: str = Form(""), token: str = Form("")):
    if mode not in MODES:
        raise HTTPException(400, "Unknown mode.")
    fmt = fmt or ("mp3" if mode == "audio" else "mp4")
    extra = quality_args(mode, quality, fmt)
    check_rate(request.client.host if request.client else "unknown")
    check_url(url)

    tmp = tempfile.mkdtemp()
    cmd = [
        "yt-dlp", *MODES[mode], *extra,
        "--no-playlist", "--restrict-filenames", "--ignore-config", "--no-continue",
        "--max-filesize", MAX_FILESIZE,
        "-o", f"{tmp}/%(title).80s.%(ext)s",
    ]
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        cmd += ["--cookies", COOKIES_FILE]
    cmd += ["--", url]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(504, "Took too long, gave up.")

    files = [p for p in Path(tmp).iterdir() if p.is_file()]
    if r.returncode != 0 or not files:
        log.warning("yt-dlp failed (%s): %s", r.returncode, r.stderr[-2000:])
        shutil.rmtree(tmp, ignore_errors=True)
        if r.returncode == 0:  # ran fine, just found nothing (e.g. no subtitles)
            raise HTTPException(404, NOTHING_FOUND.get(mode, "Nothing to download for that link."))
        raise HTTPException(502, reason(r.stderr))

    extra_cleanup = None
    if len(files) > 1:
        # e.g. subtitles in several languages — bundle them so one request = one file.
        # Written outside tmp: a zip written inside the dir it's archiving would
        # walk into itself mid-write.
        zip_path = tempfile.mktemp(suffix=".zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            for p in files:
                zf.write(p, arcname=p.name)
        stem = files[0].name.split(".")[0]  # yt-dlp's --restrict-filenames title, shared by all of them
        f = Path(zip_path)
        download_name, media_type, extra_cleanup = f"{stem}.{mode}.zip", "application/zip", zip_path
    else:
        f = files[0]
        download_name, media_type = f.name, "application/octet-stream"

    def cleanup():
        shutil.rmtree(tmp, ignore_errors=True)
        if extra_cleanup:
            Path(extra_cleanup).unlink(missing_ok=True)

    # BackgroundTask fires once the response has finished streaming — that is the
    # whole "stream then delete" story, no storage layer and no cleanup cron.
    resp = FileResponse(f, filename=download_name, media_type=media_type, background=BackgroundTask(cleanup))
    # The page submits into a hidden iframe and watches for this cookie to know
    # the file has started arriving, so it can clear its "forging" state.
    if token.isalnum() and len(token) <= 32:
        resp.set_cookie("forged", token, max_age=60)
    return resp
