# Microsoft's official Playwright image: Python + Chromium + all its system libraries preinstalled.
# Keep this tag in sync with the playwright version pinned in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.65.0-noble

WORKDIR /app
COPY requirements.txt .
# App deps, then the newest yt-dlp nightly (Facebook fixes land there first).
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt \
 && pip install --no-cache-dir --break-system-packages --upgrade --pre "yt-dlp[default]"

COPY . .
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
