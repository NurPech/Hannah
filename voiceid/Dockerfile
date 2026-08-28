FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

WORKDIR /app

# System packages: git is required for config backup/restore
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first (layer cache)
COPY requirements.txt .
# CPU-only torch wheel — the default index pulls full CUDA builds even on
# arm64, where there's no GPU to use them anyway (multi-GB, blew up CI disk)
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

LABEL org.opencontainers.image.description="VoiceID (CPU-only torch build)"

# App-Code
COPY app.py ./

# Daten-Verzeichnis (wird als Volume gemountet)
RUN useradd -r -u 1000 appuser && \
    mkdir -p /etc/hannah-voiceid /home/appuser && \
    chown -R appuser:appuser /app /etc/hannah-voiceid /home/appuser
USER appuser

CMD ["python", "app.py", "--config", "/etc/hannah-voiceid/config.yaml"]
