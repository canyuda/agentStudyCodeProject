#!/usr/bin/env bash
# Start the gateway with secrets sourced from project root .env (gitignored).
set -euo pipefail
cd "$(dirname "$0")"
set -a
source ../../.env
set +a
exec ../../.venv/bin/uvicorn gateway:app --host 127.0.0.1 --port 8000 --reload
