#!/usr/bin/env bash
# Start VigilCam.
set -e

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "No virtual environment found. Running install..."
    ./install.sh
fi

# shellcheck source=/dev/null
source .venv/bin/activate

if [ ! -f ".env" ]; then
    cp .env.example .env
    ./scripts/generate-secrets.sh .env
fi

# Load .env into environment
set -a
# shellcheck source=/dev/null
source .env
set +a

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"

case "${1:-start}" in
  start|up)
    echo "Starting VigilCam on http://${HOST}:${PORT}"
    exec python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
    ;;
  install)
    exec ./install.sh
    ;;
  token)
    python - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
    ;;
  *)
    echo "Usage: $0 {start|up|install|token}"
    exit 1
    ;;
esac
