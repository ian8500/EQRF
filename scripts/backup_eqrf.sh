#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BACKUP_DIR="${EQRF_BACKUP_DIR:-backups}"
KEEP="${EQRF_BACKUP_KEEP:-20}"
timestamp="$(date +%Y%m%d-%H%M%S)"
archive="$BACKUP_DIR/eqrf-backup-$timestamp.tar.gz"

mkdir -p "$BACKUP_DIR"

paths=(data pdfs)
if [ -d "static/jpgs" ]; then
  paths+=(static/jpgs)
fi
if [ -d "static/vendor" ]; then
  paths+=(static/vendor)
fi
if [ -d "static/pdfjs" ]; then
  paths+=(static/pdfjs)
fi

tar -czf "$archive" "${paths[@]}"
echo "Created $archive"

mapfile -t backups < <(ls -1t "$BACKUP_DIR"/eqrf-backup-*.tar.gz 2>/dev/null || true)
if [ "${#backups[@]}" -gt "$KEEP" ]; then
  for old_backup in "${backups[@]:$KEEP}"; do
    rm -f "$old_backup"
    echo "Removed old backup $old_backup"
  done
fi
