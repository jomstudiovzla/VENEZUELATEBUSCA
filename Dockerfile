FROM python:3.12-slim-bookworm

WORKDIR /app

COPY requirements-core.txt ./
RUN pip install --no-cache-dir -r requirements-core.txt

COPY . .

RUN chmod +x servidor.sh instalar.sh arrancar.sh \
    && mkdir -p config

ENV HOST=0.0.0.0
ENV PORT=8000
ENV DATABASE_URL=sqlite+aiosqlite:////data/red_esperanza.db
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

CMD ["./servidor.sh"]