#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
HOST="${HSC_HOST:-127.0.0.1}"
PORT="${PORT:-4173}"
echo "Starting AJZIO Education on http://${HOST}:${PORT}"
echo "Data is stored in: ${HSC_DATA_DIR:-$(pwd)/data}"
if [[ -n "${GOOGLE_CLIENT_ID:-}" && -n "${GOOGLE_CLIENT_SECRET:-}" && -n "${GOOGLE_REDIRECT_URI:-}" ]]; then
  echo "Google sign-in: configured"
else
  echo "Google sign-in: waiting for OAuth credentials (email/password sign-in is ready)"
fi
exec python3 server.py --host "$HOST" --port "$PORT"
