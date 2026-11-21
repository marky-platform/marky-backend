#!/bin/sh
# Arranque de producción (Dockerfile CMD). Railway inyecta $PORT.
# Corre `release` (migrate + createcachetable + seed, idempotente) antes de servir tráfico,
# así el contenedor no depende de ningún paso de pre-deploy de la plataforma.
# SKIP_RELEASE=1 lo omite (pre-deploy de la plataforma o varias réplicas).
set -e
if [ "${SKIP_RELEASE:-}" = "1" ]; then
    echo "[start] SKIP_RELEASE=1, skipping release"
else
    echo "[start] running release..."
    python manage.py release
    echo "[start] release OK"
fi
exec gunicorn marky_backend.wsgi:application \
    --bind 0.0.0.0:${PORT:-8000} \
    --workers 2 --worker-class gthread --threads 4 \
    --timeout 120 --max-requests 1000 --max-requests-jitter 100
