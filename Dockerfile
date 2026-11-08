FROM python:3.11-slim

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

COPY . .

RUN SECRET_KEY=build-time-placeholder python manage.py collectstatic --noinput

EXPOSE 8000

CMD ["gunicorn", "marky_backend.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2",
     "--worker-class", "gthread", "--threads", "4", "--timeout", "120",
     "--max-requests", "1000", "--max-requests-jitter", "100"]
