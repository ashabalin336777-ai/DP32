FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Europe/Moscow

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-dejavu-core \
        fonts-liberation \
        libcairo2 \
        libffi8 \
        libgdk-pixbuf-2.0-0 \
        libharfbuzz0b \
        libjpeg62-turbo \
        libopenjp2-7 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        shared-mime-info \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

RUN mkdir -p data/raw reports logs backups \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

COPY schema.sql ./
COPY src ./src
COPY data/scrape_targets.json data/our_catalog.html /app/data/

# Keep the service alive for `restart: unless-stopped`.
# Daily job: host cron `docker compose run --rm analyzer python src/pipeline.py`
# Repo on VPS: /opt/dp32
# or override command to `python src/scheduler.py`.
CMD ["sleep", "infinity"]
