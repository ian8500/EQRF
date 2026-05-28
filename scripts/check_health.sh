#!/usr/bin/env bash
set -euo pipefail

URL="${EQRF_HEALTH_URL:-http://127.0.0.1:8000/health}"

response="$(curl -fsS "$URL")"

case "$response" in
  *'"status":"ok"'*|*'"status": "ok"'*)
    exit 0
    ;;
  *)
    echo "EQRF health check failed: $response" >&2
    exit 1
    ;;
esac
