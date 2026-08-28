FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

WORKDIR /app

# System packages: git is required for config backup/restore
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App-Code
COPY main.py ./
COPY hannah/ hannah/

# Daten-Verzeichnis (wird als Volume gemountet)
RUN useradd -r -u 1000 appuser && \
    mkdir -p /app/activity_audio /app/audio_dumps /etc/hannah /home/appuser && \
    chown -R appuser:appuser /app /etc/hannah /home/appuser
USER appuser

EXPOSE 50051
EXPOSE 7776

VOLUME ["/app/activity_audio", "/app/audio_dumps"]

CMD ["python", "main.py", "-c", "/etc/hannah/config.yaml"]
