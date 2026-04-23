#!/usr/bin/env bash
set -e
export PATH="$PATH:/home/rene/go/bin"
cd "$(dirname "$0")"
protoc \
  -I ../core/proto \
  --go_out=proto/hannah \
  --go_opt=paths=source_relative \
  "--go_opt=Mhannah.proto=dev.kernstock.net/gessinger/voice/hannah/proxy/proto/hannah" \
  --go-grpc_out=proto/hannah \
  --go-grpc_opt=paths=source_relative \
  "--go-grpc_opt=Mhannah.proto=dev.kernstock.net/gessinger/voice/hannah/proxy/proto/hannah" \
  ../core/proto/hannah.proto
echo "✓ Proto stubs generated"
