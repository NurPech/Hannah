FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

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
