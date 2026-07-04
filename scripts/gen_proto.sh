#!/usr/bin/env bash
# Regeneriert die Python-gRPC-Stubs aus den .proto-Quelldateien.
#
# Voraussetzung: grpcio-tools ist installiert (in telegram/venv oder system-weit).
#   pip install grpcio-tools
#
# Aufruf (aus dem Repo-Root):
#   bash scripts/gen_proto.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROTO_DIR="$REPO_ROOT/proto"  # hannah-proto Submodule (gessinger/voice/hannah-proto), Source of Truth

# Python mit grpcio-tools finden
PYTHON=""
for candidate in \
    "$REPO_ROOT/telegram/venv/bin/python" \
    "$REPO_ROOT/core/venv/bin/python" \
    "python3" \
    "python"
do
    if command -v "$candidate" &>/dev/null 2>&1 || [ -x "$candidate" ]; then
        if "$candidate" -c "import grpc_tools" &>/dev/null 2>&1; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: grpcio-tools nicht gefunden. Bitte installieren:"
    echo "  pip install grpcio-tools"
    exit 1
fi

echo "Nutze Python: $PYTHON"

# Core-Stubs generieren (alle Scope-Dateien — hannah.proto #44 in mehrere
# .proto-Dateien aufgeteilt, siehe deren import-Block. Alle Dateien müssen protoc
# explizit übergeben werden, es reicht nicht die Haupt-Datei zu nennen.)
echo "→ core/hannah/proto/"
"$PYTHON" -m grpc_tools.protoc \
    -I "$PROTO_DIR" \
    --python_out="$REPO_ROOT/core/hannah/proto" \
    --grpc_python_out="$REPO_ROOT/core/hannah/proto" \
    "$PROTO_DIR"/*.proto

# Telegram-Stubs generieren (dieselbe Quelle, eigenes Ausgabe-Package)
echo "→ telegram/hannah_telegram/proto/"
"$PYTHON" -m grpc_tools.protoc \
    -I "$PROTO_DIR" \
    --python_out="$REPO_ROOT/telegram/hannah_telegram/proto" \
    --grpc_python_out="$REPO_ROOT/telegram/hannah_telegram/proto" \
    "$PROTO_DIR"/*.proto

# protoc erzeugt absolute Imports (in *_grpc.py wie auch in jeder *_pb2.py, die eine
# andere Scope-Datei importiert — seit #44 also mehrere Dateien, nicht mehr nur
# hannah_pb2_grpc.py) — innerhalb eines Python-Packages müssen diese relativ sein,
# sonst gibt es ModuleNotFoundError beim Import.
echo "→ Absolute Imports in *_pb2*.py auf relativ patchen"
sed -i -E 's/^import ([a-z0-9_]+) as ([a-z0-9_]+)$/from . import \1 as \2/' \
    "$REPO_ROOT"/core/hannah/proto/*_pb2*.py \
    "$REPO_ROOT"/telegram/hannah_telegram/proto/*_pb2*.py

# PROTO_VERSION mitkopieren — core/hannah bzw. telegram/ werden 1:1 ins
# jeweilige Release-Tarball gepackt (siehe .gitlab-ci.yml upload:core /
# upload:telegram), das proto/-Submodule selbst aber nicht. Beide Consumer
# brauchen die Datei zur Laufzeit für den Protocol-Version-Check (#60).
echo "→ PROTO_VERSION nach core/hannah/proto/ und telegram/hannah_telegram/proto/"
cp "$PROTO_DIR/PROTO_VERSION" "$REPO_ROOT/core/hannah/proto/PROTO_VERSION"
cp "$PROTO_DIR/PROTO_VERSION" "$REPO_ROOT/telegram/hannah_telegram/proto/PROTO_VERSION"

echo "Fertig."
