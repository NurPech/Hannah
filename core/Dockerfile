FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

# stdout is block-buffered when it's not a TTY (always true in a container),
# so print() output (e.g. the first-run admin account announcement in
# hannah/utils/db.py) can sit in the buffer and lag far behind logging
# module output in `docker logs`, or not show up at all before restart.
ENV PYTHONUNBUFFERED=1

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
    mkdir -p /app/activity_audio /app/audio_dumps /app/data /etc/hannah /home/appuser && \
    chown -R appuser:appuser /app /etc/hannah /home/appuser
USER appuser

# hannah.db (DB_PATH, s. hannah/utils/db.py) und der HuggingFace-Modell-Cache
# (faster-whisper) landen ohne diese Defaults außerhalb jedes Volumes und sind
# nach jedem Container-Neustart weg. memory.db liegt weiterhin unter dem
# relativen Default aus config.yaml (memory.db) — wer das persistieren will,
# zeigt memory.db in der eigenen config.yaml auf einen Pfad unter /app/data.
ENV DB_PATH=/app/data/hannah.db
ENV HF_HOME=/app/data/hf-cache

EXPOSE 50051
EXPOSE 7776

VOLUME ["/app/activity_audio", "/app/audio_dumps", "/app/data"]

CMD ["python", "main.py", "-c", "/etc/hannah/config.yaml"]
