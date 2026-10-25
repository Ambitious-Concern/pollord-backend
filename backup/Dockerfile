FROM postgres:15-alpine

# postgres:15-alpine already ships pg_basebackup + busybox crond — no extra
# packages needed.

COPY entrypoint.sh /entrypoint.sh
COPY backup.sh prune_wal.sh /scripts/
COPY crontab /etc/crontabs/root

RUN chmod +x /entrypoint.sh /scripts/backup.sh /scripts/prune_wal.sh \
    && chmod 600 /etc/crontabs/root

ENTRYPOINT ["/entrypoint.sh"]
