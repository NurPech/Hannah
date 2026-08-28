FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

WORKDIR /app

# System packages: git is required for config backup/restore
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App-Code
COPY app.py ./

# Daten-Verzeichnis (wird als Volume gemountet)
RUN useradd -r -u 1000 appuser && \
    mkdir -p /etc/hannah-voiceid /home/appuser && \
    chown -R appuser:appuser /app /etc/hannah-voiceid /home/appuser
USER appuser

CMD ["python", "app.py", "--config", "/etc/hannah-voiceid/config.yaml"]
