#!/bin/bash
set -e

# Install plugins if missing
export GOPATH="$HOME/go"
export PATH="$PATH:/usr/local/go/bin:$GOPATH/bin"

if ! which go &>/dev/null; then
  echo "ERROR: go not found in WSL"
  exit 1
fi

if ! which protoc-gen-go &>/dev/null; then
  echo "Installing protoc-gen-go..."
  go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
fi

if ! which protoc-gen-go-grpc &>/dev/null; then
  echo "Installing protoc-gen-go-grpc..."
  go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest
fi

# Generate from proxy/ (this script's own directory) — NOT the repo root.
cd "$(dirname "$0")"

mkdir -p proto/hannah

MODULE="dev.kernstock.net/gessinger/voice/hannah/proxy"
OUR_PKG="${MODULE}/proto/hannah"

# #44: hannah.proto (upstream: gessinger/voice/hannah-proto, submoduled at
# repo root as ../proto) was split by scope into multiple files (import-linked)
# — protoc needs every file listed explicitly (doesn't follow imports
# transitively for codegen) and a Go package mapping (-M) per file. Same
# PROTO_FILES list + loop structure as hannah-timer/proto/gen.sh (#45) — only
# MODULE/OUR_PKG/output dir differ between the two consumers.
PROTO_FILES=(
  shared.proto
  user_registry.proto
  control.proto
  car_state.proto
  event_stream.proto
  satellite_proxy.proto
  device_control_menu.proto
  satellite_provisioning.proto
  speaker_enrollment.proto
  agent.proto
  wakeword_capture.proto
  timer_service.proto
  hannah.proto
)

GO_OPTS=()
GRPC_OPTS=()
FILES=()
for f in "${PROTO_FILES[@]}"; do
  GO_OPTS+=(--go_opt="M${f}=${OUR_PKG}")
  GRPC_OPTS+=(--go-grpc_opt="M${f}=${OUR_PKG}")
  FILES+=("../proto/${f}")
done

protoc \
  -I ../proto \
  --go_out=. \
  --go_opt=module="${MODULE}" \
  "${GO_OPTS[@]}" \
  --go-grpc_out=. \
  --go-grpc_opt=module="${MODULE}" \
  "${GRPC_OPTS[@]}" \
  "${FILES[@]}"

# PROTO_VERSION mitkopieren — der Proxy-Release ist eine einzelne kompilierte
# Binary (siehe .gitlab-ci.yml upload:proxy:*), hat also zur Laufzeit keinen
# Zugriff auf das proto/-Submodule. version.go embedded diese lokale Kopie
# per go:embed (#60) — geht nur innerhalb des Package-Verzeichnisses, daher
# die Kopie hierher statt eines relativen Pfads zu proto/.
cp ../proto/PROTO_VERSION proto/hannah/PROTO_VERSION

echo "✓ Proto stubs generated in proto/hannah/"
ls proto/hannah/
