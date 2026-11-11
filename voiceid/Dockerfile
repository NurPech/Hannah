# Stays on Python 3.14: sentencepiece (a dependency of speechbrain) has no wheel for 3.15 yet (#502).
# renovate.json holds the base image back.
FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

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
COPY app.py log_shipping.py ./

# Release version, read by app.py (falls back to "dev" without it)
ARG VERSION=dev
RUN echo "${VERSION#v}" > VERSION

# Voice profiles: persistent copy on a volume, working copy (RAM) on a second one.
# Both directories are created and owned by appuser here, so that Docker copies
# that ownership into a fresh named volume — otherwise the volume is root-owned
# and enrollment fails with "Permission denied" (#512).
ENV VOICEID_DISK_PATH=/var/lib/hannah-voiceid/profiles \
    VOICEID_MEM_PATH=/mnt/hannah_mem
RUN useradd -r -u 1000 appuser && \
    mkdir -p /etc/hannah-voiceid /home/appuser "${VOICEID_DISK_PATH}" "${VOICEID_MEM_PATH}" && \
    chown -R appuser:appuser /app /etc/hannah-voiceid /home/appuser /var/lib/hannah-voiceid "${VOICEID_MEM_PATH}"
USER appuser

CMD ["python", "app.py", "--config", "/etc/hannah-voiceid/config.yaml"]
