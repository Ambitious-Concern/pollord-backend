# Restoring the database

This stack takes a nightly compressed base backup (`db_backup` service,
`/scripts/backup.sh`, 02:00 UTC) and continuously archives WAL segments from
the `db` service to the `wal_archive` volume. Together they support two kinds
of restore:

- **Latest nightly** — fastest, loses up to ~24h of data.
- **Point-in-time (PITR)** — restore to any specific moment (e.g. "2 minutes
  before the bad migration ran"), using the nightly backup plus WAL replay.
  This procedure has been tested end-to-end (base backup → PITR restore to an
  arbitrary timestamp → promote → verify) and is confirmed to work.

Both procedures restore into a **new, separate** data volume first, so you
can verify the restored data before touching the real `db` service.

## 1. Find your backup

```sh
docker exec pollord-db-backup ls -la /backups/base
```

Pick the timestamped directory to restore from (or the latest one).

## 2. Extract the base backup into a scratch volume

```sh
docker volume create pollord_restore_data

docker run --rm \
  -v pollord-backend_db_backups:/backups:ro \
  -v pollord_restore_data:/restore \
  alpine:3 sh -c "
    tar xzf /backups/base/<TIMESTAMP>/base.tar.gz -C /restore &&
    chown -R 70:70 /restore &&
    chmod 700 /restore
  "
```

(uid 70 is `postgres` inside the `postgres:15-alpine` image.)

## 3a. Latest-nightly restore (simplest)

```sh
docker run --rm -v pollord_restore_data:/restore alpine:3 sh -c "
  touch /restore/recovery.signal
  cat >> /restore/postgresql.auto.conf <<'EOF'
restore_command = 'cp /wal_archive/%f %p'
recovery_target_action = 'promote'
EOF
  chown 70:70 /restore/recovery.signal /restore/postgresql.auto.conf
"
```

With no `recovery_target_*` set, Postgres just replays all available WAL and
promotes at the end — i.e. "as fresh as possible."

## 3b. Point-in-time restore (recover to a specific moment)

Same as above, but add a target:

```sh
docker run --rm -v pollord_restore_data:/restore alpine:3 sh -c "
  touch /restore/recovery.signal
  cat >> /restore/postgresql.auto.conf <<'EOF'
restore_command = 'cp /wal_archive/%f %p'
recovery_target_time = '2026-01-01 12:00:00+00'
recovery_target_action = 'promote'
recovery_target_inclusive = true
EOF
  chown 70:70 /restore/recovery.signal /restore/postgresql.auto.conf
"
```

`recovery_target_time` must be a moment covered by your WAL retention window
(`RETENTION_DAYS`, default 7 days) and after the base backup you extracted.

## 4. Start a throwaway instance against the restored data

```sh
docker run -d --name restore-drill \
  -v pollord_restore_data:/var/lib/postgresql/data \
  -v pollord-backend_wal_archive:/wal_archive:ro \
  postgres:15-alpine

docker logs -f restore-drill
```

Watch for `database system is ready to accept connections`. If you asked for
a specific `recovery_target_time`, the log will show
`recovery stopping before commit of transaction ...` at (approximately) that
time — that's expected and confirms it stopped exactly where asked.

## 5. Verify

```sh
docker exec restore-drill psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "SELECT count(*) FROM votes;"
```

Check whatever tables/rows matter for the incident you're recovering from.

## 6. Promote to production (only once verified)

This is the destructive step — make sure step 5 actually checked out first.

```sh
docker compose -f docker-compose.prod.yml stop app celery_worker celery_beat db
docker volume rm pollord-backend_postgres_data
docker volume create pollord-backend_postgres_data
docker run --rm \
  -v pollord_restore_data:/from \
  -v pollord-backend_postgres_data:/to \
  alpine:3 sh -c "cp -a /from/. /to/"
docker compose -f docker-compose.prod.yml up -d db
# wait for healthy, then:
docker compose -f docker-compose.prod.yml up -d app celery_worker celery_beat
```

## 7. Clean up the drill

```sh
docker rm -f restore-drill
docker volume rm pollord_restore_data
```

## Why not just `pg_dump`?

`pg_dump` (logical backup) can't be combined with WAL replay for PITR — it's
a snapshot of data at one instant, nothing in between. `pg_basebackup`
(physical backup) plus continuous WAL archiving is what makes "restore to
any second in the last week" possible, which matters here since this is a
payments + voting system: losing an in-flight vote or charge to "restore to
last night" is worse than the extra moving parts.
