FROM python:3.11-slim

# ffmpeg is the whole reason we need a Dockerfile instead of Render's
# native Python runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PORT=8080
EXPOSE 8080

# 1 worker keeps a single in-process job queue; threads keep the webhook
# responsive while ffmpeg runs.
CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 \
    --timeout 120 --access-logfile - app:app
