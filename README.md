<p align="center"><img src="static/logo.webp" alt="LinkForge" width="220"></p>

# LinkForge

Paste a link, get the file. LinkForge is a small self-hosted web app that downloads video, audio, subtitles, thumbnails and metadata from 1800+ sites, powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp) and ffmpeg.

- **Preview first:** Analyze a link to see its title, thumbnail, length, views and the resolutions it really has.
- **Pick what you want:** video + audio, video only or audio only, with quality (resolution or bitrate) and format (MP4, WebM, MKV, MP3, M4A, Opus, FLAC).
- **More formats:** subtitles, auto captions, thumbnail or info JSON. Several subtitle languages arrive as one zip.
- **Nothing is kept:** files stream straight to the browser and are deleted from the server right after. No accounts, no database.
- **Light and dark mode**, works on mobile.

## Run it

```bash
docker run -p 8000:8000 <dockerhub-user>/linkforge
```

Or build it yourself:

```bash
docker build -t linkforge .
docker run --rm -p 8000:8000 linkforge
```

Open http://localhost:8000.

## Configuration

Set these with `-e NAME=value` on `docker run`.

| Variable | Default | What it does |
|---|---|---|
| `MAX_FILESIZE` | `500M` | Largest file yt-dlp will download. |
| `TIMEOUT` | `600` | Seconds a download may take before it is stopped. |
| `RATE_LIMIT` | `10` | Downloads per IP per hour. `0` turns it off. |
| `PREVIEW_LIMIT` | `30` | Previews per IP per hour. `0` turns it off. |
| `PREVIEW_TIMEOUT` | `20` | Seconds a preview may take. |
| `COOKIES_FILE` | empty | Path to a Netscape `cookies.txt`, for sites that need a login. |

### When YouTube says "Sign in to confirm you're not a bot"

Servers on cloud IPs get blocked sooner or later. Export `cookies.txt` from a logged-in browser and mount it:

```bash
docker run -p 8000:8000 \
  -v "$PWD/cookies.txt:/cookies.txt:ro" -e COOKIES_FILE=/cookies.txt \
  linkforge
```

### Behind a reverse proxy

Raise the proxy's read timeout to about 600 seconds. A long video can take minutes before the first byte arrives, and many proxies give up after 60. The app trusts `X-Forwarded-For` so rate limits apply to the real client IP.

## Develop

Needs Python 3.12+ and ffmpeg on your PATH.

```bash
python -m venv .venv && . .venv/bin/activate
pip install fastapi "uvicorn[standard]" yt-dlp python-multipart mutagen
uvicorn app:app --reload
```

Run the tests:

```bash
python test_app.py
```

| File | What's in it |
|---|---|
| `app.py` | FastAPI backend: `/preview`, `/download`, URL and rate-limit checks. |
| `index.html` | The whole UI: one page, no build step. |
| `static/` | Logo, mascot and favicon. |
| `test_app.py` | Tests, no framework needed. |

## Deploy to Docker Hub

`.github/workflows/docker.yml` runs the tests and pushes the image to Docker Hub on every push to `main`, tagged `latest` and with the commit SHA. Pushing a git tag like `v1.0.0` also publishes `:v1.0.0`.

One-time setup, in the GitHub repo under **Settings → Secrets and variables → Actions**:

- `DOCKERHUB_USERNAME`: your Docker Hub username.
- `DOCKERHUB_TOKEN`: a Docker Hub access token with Read & Write access.

## Safety

- Only public `http(s)` links are accepted. Local and private addresses such as `127.0.0.1` or cloud metadata IPs are rejected, so the server can't be used to reach its own network.
- Playlists are off, so one request is one file.
- yt-dlp errors are turned into plain messages, and raw output (which can include paths or cookies) never reaches the browser.

Only download what you have the right to download.
