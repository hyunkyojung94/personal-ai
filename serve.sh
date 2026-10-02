#!/usr/bin/env bash
# Run the local model server. Only reachable from this machine, and every
# request must carry the API key (otherwise any web page open in your browser
# could call it).
set -euo pipefail

DATA_DIR="${PERSONAL_AI_DATA:-$HOME/personal-ai-data}"
MODEL="${MODEL:-$HOME/models/gemma-4-26B_q4_0-it.gguf}"
KEY_FILE="$DATA_DIR/api-key"

if [ ! -f "$KEY_FILE" ]; then
  mkdir -p "$DATA_DIR"
  (umask 077 && openssl rand -hex 32 > "$KEY_FILE")
  echo "Created API key at $KEY_FILE"
fi

exec llama-server -m "$MODEL" \
  --host 127.0.0.1 --port 8080 \
  -c 16384 -ngl 99 --jinja \
  --api-key-file "$KEY_FILE"
