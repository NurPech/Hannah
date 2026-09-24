FROM python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2

WORKDIR /app

# System packages: git is required for config backup/restore
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App-Code
COPY main.py ./
COPY hannah_telegram/ hannah_telegram/

# Release version, read by hannah_telegram/version.py (falls back to "dev" without it)
ARG VERSION=dev
RUN echo "${VERSION#v}" > VERSION

# Daten-Verzeichnis (wird als Volume gemountet)
RUN useradd -r -u 1000 appuser && \
    mkdir -p /etc/hannah-telegram /home/appuser && \
    chown -R appuser:appuser /app /etc/hannah-telegram /home/appuser
USER appuser

CMD ["python", "main.py", "--config", "/etc/hannah-telegram/config.yaml"]
