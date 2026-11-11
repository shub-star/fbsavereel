FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
# 1) App deps + newest yt-dlp nightly (Facebook fixes land there first).
# 2) Headless Chrome (Playwright's lightweight "headless shell") + its system libraries.
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir --upgrade --pre "yt-dlp[default,curl-cffi]" \
 && playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/*

COPY . .
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
