#!/usr/bin/env bash
# Generate local secrets for VigilCam.
set -e
cd "$(dirname "$0")/.."

ENV_FILE="${1:-.env}"

if [ ! -f "$ENV_FILE" ]; then
    cp .env.example "$ENV_FILE"
fi

python3 - "$ENV_FILE" <<'PY'
import secrets, re, sys
path = sys.argv[1]
text = open(path).read()
key = secrets.token_urlsafe(48)
if "SECRET_KEY=" in text:
    text = re.sub(r"^SECRET_KEY=.*$", f"SECRET_KEY={key}", text, count=1, flags=re.M)
else:
    text += f"\nSECRET_KEY={key}\n"
open(path, "w").write(text)
print(f"Updated SECRET_KEY in {path}")
PY
