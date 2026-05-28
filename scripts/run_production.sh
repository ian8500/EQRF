#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ -f "$ROOT_DIR/venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$ROOT_DIR/venv/bin/activate"
fi

export EQRF_SECRET_KEY="${EQRF_SECRET_KEY:-change-this}"
export EQRF_PASSWORD="${EQRF_PASSWORD:-change-this}"
export FLASK_DEBUG="${FLASK_DEBUG:-0}"
export FLASK_RUN_HOST="${FLASK_RUN_HOST:-0.0.0.0}"
export FLASK_RUN_PORT="${FLASK_RUN_PORT:-8000}"
export EQRF_GUNICORN_WORKERS="${EQRF_GUNICORN_WORKERS:-2}"

exec gunicorn \
  -w "$EQRF_GUNICORN_WORKERS" \
  -b "${FLASK_RUN_HOST}:${FLASK_RUN_PORT}" \
  wsgi:application
