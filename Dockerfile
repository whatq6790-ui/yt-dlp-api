FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg aria2 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Always upgrade yt-dlp to the latest version at build time
RUN pip install --no-cache-dir --upgrade yt-dlp

COPY app.py .

EXPOSE 8000
CMD pip install --no-cache-dir --upgrade --quiet yt-dlp && gunicorn app:app --bind 0.0.0.0:8000 --timeout 300 --workers 1 --worker-class gthread --threads 8
