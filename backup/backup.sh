#!/bin/sh
set -eu
. /etc/backup.env

log() { echo "[$(date -Iseconds)] $*" > /proc/1/fd/1; }

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/backups/base/${TIMESTAMP}"

log "Starting base backup -> ${BACKUP_DIR}"
mkdir -p "${BACKUP_DIR}"

# -Ft -z: tar format, gzip-compressed (compact, easy to ship offsite later).
# -Xs: stream WAL alongside the backup so it's self-contained even if the
# archive_command hiccups during the backup window.
if pg_basebackup -h "${POSTGRES_HOST}" -U "${POSTGRES_USER}" -D "${BACKUP_DIR}" -Ft -z -Xs -P >> /proc/1/fd/1 2>&1; then
  log "Base backup complete."
else
  log "Base backup FAILED — removing partial directory."
  rm -rf "${BACKUP_DIR}"
  exit 1
fi

find /backups/base -mindepth 1 -maxdepth 1 -type d -mtime "+${RETENTION_DAYS}" -print -exec rm -rf {} \; >> /proc/1/fd/1 2>&1
log "Pruned base backups older than ${RETENTION_DAYS} days."
