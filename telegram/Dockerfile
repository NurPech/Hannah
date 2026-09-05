FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

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

# Daten-Verzeichnis (wird als Volume gemountet)
RUN useradd -r -u 1000 appuser && \
    mkdir -p /etc/hannah-telegram /home/appuser && \
    chown -R appuser:appuser /app /etc/hannah-telegram /home/appuser
USER appuser

CMD ["python", "main.py", "--config", "/etc/hannah-telegram/config.yaml"]
