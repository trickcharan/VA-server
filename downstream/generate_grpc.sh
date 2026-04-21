#!/bin/bash
# Fetch proto schemas and generate gRPC Python code for the downstream module.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROTO_DIR="$SCRIPT_DIR/proto"

echo "Fetching proto schemas..."
python "$SCRIPT_DIR/fetch_proto_schema.py"

echo "Generating gRPC code from proto files..."
python -m grpc_tools.protoc \
    -I"$PROTO_DIR" \
    --python_out="$PROTO_DIR" \
    --grpc_python_out="$PROTO_DIR" \
    "$PROTO_DIR"/*.proto

echo "Done."
