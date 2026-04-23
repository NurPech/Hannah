#!/bin/bash
# Hannah Satellit — Installations-Skript
# Erkennt Plattform und Python-Version automatisch und wählt den richtigen Weg.
#
# Verwendung:
#   bash install.sh
#   bash install.sh --venv   # legt zuerst ein venv an

set -e

PYTHON=${PYTHON:-python3}
ARCH=$(uname -m)
PY_VER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MINOR=$($PYTHON -c "import sys; print(sys.version_info.minor)")

echo "==> Hannah Satellit Install"
echo "    Plattform : $ARCH"
echo "    Python    : $PY_VER"
echo ""

# Optional: venv anlegen
if [[ "${1:-}" == "--venv" ]]; then
    if [[ ! -d venv ]]; then
        echo "==> Erstelle venv ..."
        $PYTHON -m venv venv
    fi
    source venv/bin/activate
    PYTHON=python
    echo "    venv aktiviert."
    echo ""
fi

PIP="$PYTHON -m pip install --prefer-binary"

# System-Voraussetzungen prüfen
echo "==> Systempackete prüfen (portaudio19-dev, libopenblas0) ..."
if ! dpkg -s portaudio19-dev &>/dev/null 2>&1; then
    echo "    FEHLER: portaudio19-dev fehlt. Bitte installieren:"
    echo "    sudo apt install portaudio19-dev libopenblas0"
    exit 1
fi
echo "    OK"
echo ""

# Basis-Pakete
echo "==> Basis-Pakete installieren ..."
$PIP -r requirements.txt
echo ""

# openwakeword: plattformabhängig
echo "==> openwakeword installieren ($ARCH / Python $PY_VER) ..."

if [[ "$ARCH" == "armv7l" ]]; then
    # 32-bit RPi (Pi 2B, Pi 3B 32-bit): tflite-runtime via Google Coral
    echo "    Modus: tflite (armv7l)"
    $PIP --extra-index-url https://google-coral.github.io/py-repo/ tflite-runtime
    $PIP --no-deps openwakeword
    $PIP scipy requests tqdm scikit-learn
    echo ""
    echo "    Satellit starten mit: --framework tflite"

elif [[ "$ARCH" == "aarch64" ]] && [[ "$PY_MINOR" -ge 13 ]]; then
    # 64-bit RPi mit Python >= 3.13: kein tflite-runtime-Wheel verfügbar → onnxruntime
    echo "    Modus: onnxruntime (aarch64 / Python >= 3.13)"
    $PIP --no-deps openwakeword
    $PIP onnxruntime scipy requests tqdm scikit-learn

else
    # aarch64 Python < 3.13 oder x86_64: normales openwakeword
    echo "    Modus: onnxruntime (Standard)"
    $PIP openwakeword
fi

echo ""
echo "==> Fertig!"
echo ""
echo "Wake-Word-Modelle herunterladen (einmalig):"
echo "  $PYTHON satelite.py --download-models"
echo ""
echo "Satellit starten:"
echo "  $PYTHON satelite.py --device <name> --room <raum> --broker <ip>"
