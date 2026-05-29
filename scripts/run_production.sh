#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -f "$ROOT_DIR/venv/bin/activate" ]; then
  echo "ERROR: Python virtual environment not found at $ROOT_DIR/venv." >&2
  echo "Create it with: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "$ROOT_DIR/venv/bin/activate"

export EQRF_SECRET_KEY="${EQRF_SECRET_KEY:-change-this}"
export EQRF_PASSWORD="${EQRF_PASSWORD:-change-this}"
export FLASK_DEBUG="${FLASK_DEBUG:-0}"
export FLASK_RUN_HOST="${FLASK_RUN_HOST:-0.0.0.0}"
export FLASK_RUN_PORT="${FLASK_RUN_PORT:-8000}"
export GUNICORN_WORKERS="${GUNICORN_WORKERS:-1}"
export GUNICORN_THREADS="${GUNICORN_THREADS:-4}"
export GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-0}"

exec gunicorn \
  --worker-class gthread \
  -w "$GUNICORN_WORKERS" \
  --threads "$GUNICORN_THREADS" \
  --timeout "$GUNICORN_TIMEOUT" \
  -b "${FLASK_RUN_HOST}:${FLASK_RUN_PORT}" \
  wsgi:application
