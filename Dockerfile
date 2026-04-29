FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WENKU_HOST=0.0.0.0 \
    WENKU_PORT=5000 \
    WENKU_DATA_DIR=/app/data \
    WENKU_ENABLE_SERVER_ADMIN=0 \
    WENKU_CORS_ORIGINS=null,http://localhost,http://127.0.0.1

WORKDIR /app

COPY requirements_wenku_to_pdf.txt /app/requirements.txt

RUN python -m pip install --upgrade pip \
    && python -m pip install -r /app/requirements.txt \
    && python -m playwright install --with-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

RUN mkdir -p /app/data/downloads

EXPOSE 5000

CMD ["python", "app.py"]
