FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg decodes phone recordings; Pango/HarfBuzz lay out Arabic text in PDFs;
# Noto provides Arabic and Latin fonts (trimmed to the families the exports use).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg fontconfig fonts-noto-core libharfbuzz-subset0 libpango-1.0-0 libpangoft2-1.0-0 \
 && find /usr/share/fonts/truetype/noto -type f \
      ! -name 'NotoSans-*' ! -name 'NotoSansArabic-*' ! -name 'NotoNaskhArabic-*' -delete \
 && fc-cache -f \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

RUN useradd --create-home --uid 10001 app
USER app

# Cloud Run sets PORT. Migrations run at startup (guarded by a Postgres advisory lock);
# set RUN_MIGRATIONS=false to run them separately instead.
ENV PORT=8080 RUN_MIGRATIONS=true
CMD ["sh", "-c", "if [ \"$RUN_MIGRATIONS\" = true ]; then alembic upgrade head; fi && exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port \"$PORT\" --proxy-headers --forwarded-allow-ips '*'"]
