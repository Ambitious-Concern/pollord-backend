``#!/bin/sh
set -eu

# Cron jobs don't reliably inherit the container's environment (varies by
# busybox crond version), so write the secret out to a .pgpass file once at
# startup instead of relying on env vars being visible at cron-execution
# time. Non-secret settings go in a plain sourceable file the scripts load.
echo "${POSTGRES_HOST:-db}:5432:*:${POSTGRES_USER}:${POSTGRES_PASSWORD}" > /root/.pgpass
chmod 600 /root/.pgpass

cat > /etc/backup.env <<EOF
POSTGRES_HOST=${POSTGRES_HOST:-db}
POSTGRES_USER=${POSTGRES_USER}
POSTGRES_DB=${POSTGRES_DB}
RETENTION_DAYS=${RETENTION_DAYS:-7}
EOF

echo "[$(date -Iseconds)] db_backup container started. Cron schedule:"
cat /etc/crontabs/root

exec crond -f -l 2
