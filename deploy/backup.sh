#!/bin/sh
set -eu

ROOT="${1:-/opt/dp32}"
DB="${ROOT}/data/mcu_competitors.db"
DEST="${ROOT}/backups"
STAMP="$(date +%F)"

mkdir -p "$DEST"

if [ ! -f "$DB" ]; then
  echo "skip backup: $DB not found"
  exit 0
fi

cp "$DB" "$DEST/mcu_${STAMP}.db"
echo "backup $DEST/mcu_${STAMP}.db"
