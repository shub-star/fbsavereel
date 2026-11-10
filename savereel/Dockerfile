FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
# --upgrade yt-dlp on every build: Facebook changes often and old versions break.
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir --upgrade yt-dlp

COPY . .
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
