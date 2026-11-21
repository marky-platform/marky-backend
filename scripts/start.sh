#!/bin/sh
# Arranque de producción (Dockerfile CMD). Railway inyecta $PORT.
set -e
exec gunicorn marky_backend.wsgi:application \
    --bind 0.0.0.0:${PORT:-8000} \
    --workers 2 --worker-class gthread --threads 4 \
    --timeout 120 --max-requests 1000 --max-requests-jitter 100
