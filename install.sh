#!/usr/bin/env bash
# Install VigilCam into a local Python virtual environment.
set -e

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required but was not found." >&2
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Using Python $PY_VERSION"

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

# shellcheck source=/dev/null
source .venv/bin/activate

python -m pip install --upgrade pip >/dev/null
pip install -r requirements.txt

mkdir -p data/recordings data/snapshots

if [ ! -f ".env" ]; then
    cp .env.example .env
fi

if grep -q '^SECRET_KEY=change-me' .env; then
    ./scripts/generate-secrets.sh .env
fi

echo
echo "VigilCam installed."
echo "Run: ./start.sh"
