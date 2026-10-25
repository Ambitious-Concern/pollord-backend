#!/bin/sh
set -eu
. /etc/backup.env

log() { echo "[$(date -Iseconds)] $*" > /proc/1/fd/1; }

# Keep the same retention window as base backups — WAL older than the
# oldest retained base backup can't be replayed against anything we still
# have anyway, so there's no PITR value in keeping it longer.
OLDEST_BASE=$(find /backups/base -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort | head -n1)
if [ -z "${OLDEST_BASE}" ]; then
  log "No base backups yet; skipping WAL prune (keeping everything for now)."
  exit 0
fi

find /wal_archive -type f -mtime "+${RETENTION_DAYS}" -print -delete >> /proc/1/fd/1 2>&1
log "Pruned WAL archives older than ${RETENTION_DAYS} days."
