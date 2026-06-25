FROM python:3.12-slim-bookworm

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-core.txt ./
RUN pip install --no-cache-dir -r requirements-core.txt

COPY . .

RUN chmod +x servidor.sh instalar.sh arrancar.sh \
    && mkdir -p reference_photos building_photos camera_snapshots snapshots

ENV HOST=0.0.0.0
ENV PORT=8000
ENV SCRAPER_POLL_INTERVAL=20
ENV TERREMOTO_POLL_INTERVAL=20
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

CMD ["./servidor.sh"]