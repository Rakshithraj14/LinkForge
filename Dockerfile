FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JS runtime to solve YouTube's player challenges; deno is its default.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

RUN pip install --no-cache-dir fastapi uvicorn[standard] yt-dlp python-multipart mutagen

RUN useradd -m app
WORKDIR /app
COPY app.py index.html ./
COPY static ./static
USER app

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
