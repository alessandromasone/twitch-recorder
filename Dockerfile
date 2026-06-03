FROM python:3.12-slim AS base
WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/

RUN mkdir -p /data/recordings

ENV CHANNELS_FILE=/data/channels.json \
    RECORDINGS_DIR=/data/recordings \
    STREAM_QUALITY=best \
    CHECK_INTERVAL=60 \
    PORT=5000 \
    MAX_FILE_SIZE=1932735283 \
    LOG_LEVEL=INFO

EXPOSE 5000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')" || exit 1

ENTRYPOINT ["tini", "--"]
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--timeout", "120", "app:app"]
