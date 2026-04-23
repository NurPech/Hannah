#!/usr/bin/env bash
# Generiert Python-Stubs aus hannah.proto und patcht den relativen Import
# (grpc_tools generiert absoluten Import, der im Package nicht funktioniert).
set -e
cd "$(dirname "$0")/.."

python -m grpc_tools.protoc \
  -I proto \
  --python_out=hannah/proto \
  --grpc_python_out=hannah/proto \
  proto/hannah.proto

# grpc_tools-Bug: "import hannah_pb2" → "from . import hannah_pb2"
sed -i 's/^import hannah_pb2/from . import hannah_pb2/' hannah/proto/hannah_pb2_grpc.py

echo "✓ Python proto stubs generated in hannah/proto/"
