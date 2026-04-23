#!/bin/bash
set -e

echo "--- Hannah Voice-ID Setup ---"

# 1. RAM-Disk erstellen (falls nicht vorhanden)
if [ ! -d "/mnt/hannah_mem" ]; then
    sudo mkdir -p /mnt/hannah_mem
    sudo mount -t tmpfs -o size=128M tmpfs /mnt/hannah_mem
    echo "Mounter RAM-Disk: 128MB auf /mnt/hannah_mem"
fi

# 2. Python Environment einrichten
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install fastapi uvicorn python-multipart

echo "--- Fertig! Starte den Service mit: source venv/bin/activate && python app.py ---"