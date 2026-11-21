FROM python:3.11.18-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    binutils \
    gdal-bin \
    libgdal-dev \
    libgeos-dev \
    libproj-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run as an unprivileged user. /app/media is pre-created and owned by it so a
# mounted volume (docker-compose) or local MEDIA_ROOT stays writable, and
# collectstatic below writes /app/staticfiles as the same user.
RUN useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && mkdir -p /app/media /app/staticfiles \
    && chown -R app:app /app

COPY --chown=app:app . .

USER app

# Build-time placeholders only (not persisted in the image env): settings.py
# fails closed with DEBUG=False unless the prod env vars exist; collectstatic
# never connects to the DB or any external service.
RUN SECRET_KEY=build-time-placeholder \
    DATABASE_URL=postgis://build:build@localhost:5432/build \
    HOST=localhost FRONTEND_URL=http://localhost \
    RESEND_API_KEY=build DEFAULT_FROM_EMAIL=build@localhost AWS_ACCESS_KEY_ID=build AWS_SECRET_ACCESS_KEY=build \
    AWS_STORAGE_BUCKET_NAME=build AWS_S3_ENDPOINT_URL=http://localhost \
    python manage.py collectstatic --noinput

EXPOSE 8000

CMD ["gunicorn", "marky_backend.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", \
     "--worker-class", "gthread", "--threads", "4", "--timeout", "120", \
     "--max-requests", "1000", "--max-requests-jitter", "100"]
