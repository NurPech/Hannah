FROM python:3.15-slim@sha256:8c13a3f37a6a5b71498228e902c4b9f51c1f7900d49fabf71f9a650c6120e0d0

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
