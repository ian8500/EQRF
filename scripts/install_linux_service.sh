#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -f "app.py" ] || [ ! -f "wsgi.py" ] || [ ! -f "requirements.txt" ]; then
  echo "ERROR: Run this script from inside the EQRF repository." >&2
  exit 1
fi

if [ ! -d "venv" ]; then
  python3 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate
pip install -r requirements.txt

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Edit EQRF_SECRET_KEY and EQRF_PASSWORD before operational use."
fi

echo
echo "This will install/update /etc/systemd/system/eqrf.service from deploy/eqrf.service.example."
echo "Review deploy/eqrf.service.example first, especially User, Group, WorkingDirectory, and EnvironmentFile."
read -r -p "Continue? [y/N] " answer
case "$answer" in
  y|Y|yes|YES)
    sudo cp deploy/eqrf.service.example /etc/systemd/system/eqrf.service
    sudo systemctl daemon-reload
    sudo systemctl enable eqrf
    sudo systemctl restart eqrf
    ;;
  *)
    echo "Service install cancelled."
    exit 0
    ;;
esac

echo
echo "Check service status:"
echo "  sudo systemctl status eqrf"
echo
echo "Follow logs:"
echo "  journalctl -u eqrf -f"
