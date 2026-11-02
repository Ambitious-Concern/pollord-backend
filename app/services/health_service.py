"""System health probes: database, Redis, SMTP, Celery.

Each probe measures point-in-time latency, pushes it into a Redis ring buffer
(last 60 readings), and returns p50/p95/p99 computed from that buffer so the
frontend can show real percentile data without a separate metrics system.
"""
import asyncio
import json
import re
import socket
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import get_redis

_RING = 60  # readings to keep per service
_HC = "health:cache:"  # prefix for response-level cache keys


# ── Response cache helpers ─────────────────────────────────────────────────────

async def _hget(key: str) -> Any | None:
    try:
        r = await get_redis()
        v = await r.get(_HC + key)
        return json.loads(v) if v else None
    except Exception:
        return None


async def _hset(key: str, value: Any, ttl: int) -> None:
    try:
        r = await get_redis()
        await r.set(_HC + key, json.dumps(value, default=str), ex=ttl)
    except Exception:
        pass


async def invalidate_health_cache() -> None:
    try:
        r = await get_redis()
        keys = await r.keys(_HC + "*")
        route_keys = await r.keys("health:route:*")
        all_keys = (keys or []) + (route_keys or [])
        if all_keys:
            await r.delete(*all_keys)
    except Exception:
        pass


async def _push_latency(service: str, ms: float) -> None:
    try:
        r = await get_redis()
        key = f"health:latency:{service}"
        await r.rpush(key, ms)
        await r.ltrim(key, -_RING, -1)
    except Exception:
        pass


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    i = min(int(len(s) * p / 100), len(s) - 1)
    return round(s[i], 2)


async def _latency_stats(service: str) -> dict[str, Any]:
    try:
        r = await get_redis()
        raw = await r.lrange(f"health:latency:{service}", 0, -1)
        vals = [float(v) for v in raw]
        return {
            "p50": _pct(vals, 50),
            "p95": _pct(vals, 95),
            "p99": _pct(vals, 99),
            "samples": len(vals),
        }
    except Exception:
        return {"p50": 0, "p95": 0, "p99": 0, "samples": 0}


# ── Database ───────────────────────────────────────────────────────────────────

async def probe_database(db: AsyncSession) -> dict[str, Any]:
    t0 = time.perf_counter()
    status = "healthy"
    error = None
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        status = "down"
        error = str(exc)
    ms = round((time.perf_counter() - t0) * 1000, 2)
    await _push_latency("database", ms)

    pool: dict[str, Any] = {}
    try:
        p = db.get_bind().pool
        pool = {
            "size": p.size(),
            "checked_in": p.checkedin(),
            "checked_out": p.checkedout(),
            "overflow": p.overflow(),
        }
    except Exception:
        pass

    return {
        "status": status,
        "latency_ms": ms,
        "error": error,
        "pool": pool,
        "latency_stats": await _latency_stats("database"),
    }


# ── Redis ──────────────────────────────────────────────────────────────────────

async def probe_redis() -> dict[str, Any]:
    t0 = time.perf_counter()
    status = "healthy"
    error = None
    info: dict[str, Any] = {}
    ms = 0.0
    try:
        r = await get_redis()
        await r.ping()
        ms = round((time.perf_counter() - t0) * 1000, 2)
        await _push_latency("redis", ms)

        raw = await r.info()
        ks = await r.info("keyspace")
        info = {
            "version": raw.get("redis_version", "?"),
            "used_memory_human": raw.get("used_memory_human", "?"),
            "used_memory_mb": round(raw.get("used_memory", 0) / 1024 / 1024, 2),
            "connected_clients": raw.get("connected_clients", 0),
            "instantaneous_ops_per_sec": raw.get("instantaneous_ops_per_sec", 0),
            "total_commands_processed": raw.get("total_commands_processed", 0),
            "uptime_in_seconds": raw.get("uptime_in_seconds", 0),
            "keyspace": {k: v for k, v in ks.items() if k.startswith("db")},
        }
    except Exception as exc:
        status = "down"
        error = str(exc)
        ms = round((time.perf_counter() - t0) * 1000, 2)

    return {
        "status": status,
        "latency_ms": ms,
        "error": error,
        "info": info,
        "latency_stats": await _latency_stats("redis"),
    }


# ── SMTP ───────────────────────────────────────────────────────────────────────

async def probe_smtp() -> dict[str, Any]:
    hit = await _hget("smtp")
    if hit:
        return hit

    def _tcp() -> str | None:
        try:
            with socket.create_connection((settings.MAIL_HOST, settings.MAIL_PORT), timeout=1):
                return None
        except Exception as exc:
            return str(exc)

    t0 = time.perf_counter()
    loop = asyncio.get_running_loop()
    err = await loop.run_in_executor(None, _tcp)
    ms = round((time.perf_counter() - t0) * 1000, 2)
    await _push_latency("smtp", ms)

    encryption = "SSL" if settings.MAIL_USE_SSL else ("STARTTLS" if settings.MAIL_USE_TLS else "None")
    result = {
        "status": "healthy" if err is None else "down",
        "latency_ms": ms,
        "host": settings.MAIL_HOST,
        "port": settings.MAIL_PORT,
        "encryption": encryption,
        "from_address": settings.MAIL_FROM,
        "from_name": settings.MAIL_FROM_NAME,
        "username": settings.MAIL_USERNAME,
        "auth": bool(settings.MAIL_USERNAME),
        "error": err,
        "latency_stats": await _latency_stats("smtp"),
    }
    await _hset("smtp", result, 60)
    return result


# ── Celery ─────────────────────────────────────────────────────────────────────

_KNOWN_QUEUES = ["celery"]


async def _queue_depths() -> dict[str, int]:
    try:
        r = await get_redis()
        return {q: await r.llen(q) for q in _KNOWN_QUEUES}
    except Exception:
        return {}


async def probe_celery() -> dict[str, Any]:
    hit = await _hget("celery")
    if hit:
        return hit

    from app.tasks.celery_app import celery_app

    # Each call gets its own inspect instance so they can run in parallel safely.
    def _get_active() -> dict:
        return celery_app.control.inspect(timeout=0.3).active() or {}

    def _get_reserved() -> dict:
        return celery_app.control.inspect(timeout=0.3).reserved() or {}

    def _get_stats() -> dict:
        return celery_app.control.inspect(timeout=0.3).stats() or {}

    t0 = time.perf_counter()
    loop = asyncio.get_running_loop()
    try:
        active, reserved, stats = await asyncio.gather(
            loop.run_in_executor(None, _get_active),
            loop.run_in_executor(None, _get_reserved),
            loop.run_in_executor(None, _get_stats),
        )
        all_workers = set(list(active) + list(stats))
        result: dict[str, Any] = {
            "workers": list(all_workers),
            "worker_count": len(all_workers),
            "active_tasks": sum(len(v) for v in active.values()),
            "reserved_tasks": sum(len(v) for v in reserved.values()),
            "worker_details": {
                name: {
                    "active": len(active.get(name, [])),
                    "reserved": len(reserved.get(name, [])),
                    "total_tasks": stats[name].get("total", {}) if name in stats else {},
                    "pid": stats[name].get("pid") if name in stats else None,
                    "concurrency": (
                        stats[name].get("pool", {}).get("max-concurrency")
                        if name in stats else None
                    ),
                }
                for name in all_workers
            },
            "error": None,
        }
    except Exception as exc:
        result = {
            "workers": [],
            "worker_count": 0,
            "active_tasks": 0,
            "reserved_tasks": 0,
            "worker_details": {},
            "error": str(exc),
        }
    ms = round((time.perf_counter() - t0) * 1000, 2)

    payload = {
        "status": "healthy" if result["worker_count"] > 0 else "degraded",
        "latency_ms": ms,
        "queues": await _queue_depths(),
        **result,
    }
    await _hset("celery", payload, 30)
    return payload


# ── Beat schedule ──────────────────────────────────────────────────────────────

def beat_schedule() -> list[dict[str, str]]:
    from app.tasks.celery_app import celery_app

    return [
        {"name": name, "task": entry["task"], "schedule": str(entry["schedule"])}
        for name, entry in celery_app.conf.beat_schedule.items()
    ]


# ── Server metrics ─────────────────────────────────────────────────────────────

def _cpu_model_name() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    import platform
    return platform.processor() or "Unknown CPU"


def _collect_server_metrics() -> dict[str, Any]:
    """Blocking — call via run_in_executor."""
    try:
        import socket as _socket
        import time as _time
        import platform
        import psutil
        from datetime import datetime as _dt, timezone as _tz

        # interval=0.1 blocks for 100ms — kept in thread, not event loop
        cpu_pct = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")

        boot_ts = psutil.boot_time()
        uptime_s = int(_time.time() - boot_ts)
        days, rem = divmod(uptime_s, 86400)
        hours = rem // 3600
        since = _dt.fromtimestamp(boot_ts, tz=_tz.utc).strftime("%d %b, %H:%M")

        return {
            "hostname": _socket.gethostname(),
            "platform": f"{platform.system().lower()}/{platform.machine().lower()}",
            "cpu": {
                "percent": round(cpu_pct, 1),
                "cores": psutil.cpu_count(logical=True),
                "model": _cpu_model_name(),
            },
            "ram": {
                "percent": round(mem.percent, 1),
                "used_mb": round(mem.used / 1024 / 1024, 1),
                "total_gb": round(mem.total / 1024 / 1024 / 1024, 1),
            },
            "disk": {
                "percent": round(disk.percent, 1),
                "used_gb": round(disk.used / 1024 / 1024 / 1024, 1),
                "total_gb": round(disk.total / 1024 / 1024 / 1024, 1),
            },
            "uptime": {
                "seconds": uptime_s,
                "formatted": f"{days}d {hours}h",
                "since": since,
            },
        }
    except ImportError:
        return {}
    except Exception:
        return {}


async def server_metrics() -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _collect_server_metrics)


# ── Route performance ──────────────────────────────────────────────────────────

async def route_stats(limit: int = 30) -> list[dict[str, Any]]:
    """Return per-route performance aggregates from Redis (populated by RouteTrackerMiddleware)."""
    try:
        r = await get_redis()
        keys = await r.keys("health:route:*")
        if not keys:
            return []

        routes = []
        for raw_key in keys:
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            data = await r.hgetall(key)
            if not data:
                continue

            def _int(field: str) -> int:
                v = data.get(field) or data.get(field.encode())
                return int(v) if v else 0

            def _float(field: str) -> float:
                v = data.get(field) or data.get(field.encode())
                return float(v) if v else 0.0

            calls = _int("calls")
            if calls == 0:
                continue

            total_ms = _float("total_ms")
            max_ms = _float("max_ms")
            errors = _int("errors")
            last_status = _int("last_status")
            avg_ms = round(total_ms / calls, 2)

            # key: health:route:{METHOD}:{/path} — split on first 3 colons
            parts = key.split(":", 3)
            method = parts[2] if len(parts) > 2 else "?"
            path = parts[3] if len(parts) > 3 else key

            routes.append({
                "method": method,
                "path": path,
                "calls": calls,
                "avg_ms": avg_ms,
                "max_ms": round(max_ms, 2),
                "errors": errors,
                "error_rate": round(errors / calls * 100, 1),
                "last_status": last_status,
            })

        routes.sort(key=lambda x: x["avg_ms"], reverse=True)
        return routes[:limit]
    except Exception:
        return []


# ── Slow queries ───────────────────────────────────────────────────────────────

async def slow_queries(db: AsyncSession, limit: int = 20) -> list[dict[str, Any]]:
    """Return top slow queries from pg_stat_statements; empty list if unavailable."""
    key = f"slow_queries:{limit}"
    hit = await _hget(key)
    if hit is not None:
        return hit

    try:
        result = await db.execute(text("""
            SELECT
                queryid,
                LEFT(query, 300)              AS query,
                calls,
                ROUND(mean_exec_time::numeric, 2) AS mean_exec_ms,
                ROUND(max_exec_time::numeric,  2) AS max_exec_ms,
                ROUND(total_exec_time::numeric, 2) AS total_exec_ms,
                ROUND(stddev_exec_time::numeric, 2) AS stddev_ms,
                rows
            FROM pg_stat_statements
            WHERE query NOT ILIKE '%pg_stat_statements%'
              AND query NOT ILIKE 'COMMIT%'
              AND query NOT ILIKE 'BEGIN%'
              AND query NOT ILIKE 'SET %'
              AND calls > 1
            ORDER BY mean_exec_time DESC
            LIMIT :limit
        """), {"limit": limit})
        rows = [dict(r) for r in result.mappings().all()]
    except Exception:
        rows = []

    await _hset(key, rows, 60)
    return rows


# ── DB table stats ─────────────────────────────────────────────────────────────

async def db_table_stats(db: AsyncSession) -> list[dict[str, Any]]:
    hit = await _hget("db_tables")
    if hit is not None:
        return hit

    result = await db.execute(text("""
        SELECT
            t.relname AS table_name,
            pg_total_relation_size(t.oid) AS total_bytes,
            pg_relation_size(t.oid) AS table_bytes,
            pg_indexes_size(t.oid) AS indexes_bytes,
            COALESCE(s.n_live_tup, 0) AS live_rows,
            COALESCE(s.n_dead_tup, 0) AS dead_rows,
            COALESCE(s.seq_scan, 0) AS seq_scan,
            COALESCE(s.idx_scan, 0) AS idx_scan,
            s.last_vacuum,
            s.last_autovacuum,
            s.last_analyze,
            s.last_autoanalyze,
            COALESCE(io.heap_blks_read, 0) AS heap_blks_read,
            COALESCE(io.heap_blks_hit, 0) AS heap_blks_hit
        FROM pg_class t
        JOIN pg_namespace n ON n.oid = t.relnamespace
        LEFT JOIN pg_stat_user_tables s ON s.relname = t.relname AND s.schemaname = 'public'
        LEFT JOIN pg_statio_user_tables io ON io.relname = t.relname AND io.schemaname = 'public'
        WHERE n.nspname = 'public' AND t.relkind = 'r'
        ORDER BY pg_total_relation_size(t.oid) DESC
    """))
    rows = []
    for r in result.mappings().all():
        d = dict(r)
        live = d["live_rows"]
        dead = d["dead_rows"]
        total_rows = live + dead
        dead_ratio = round(100.0 * dead / total_rows, 1) if total_rows > 0 else 0.0
        seq = d["seq_scan"]
        idx = d["idx_scan"]
        idx_ratio = round(100.0 * idx / (seq + idx), 1) if (seq + idx) > 0 else None
        reads = d["heap_blks_read"]
        hits = d["heap_blks_hit"]
        cache_hit = round(100.0 * hits / (reads + hits), 2) if (reads + hits) > 0 else None
        last_vac = d.get("last_vacuum") or d.get("last_autovacuum")
        last_ana = d.get("last_analyze") or d.get("last_autoanalyze")
        hints, health = [], "healthy"
        if dead_ratio > 20:
            health = "bloated"
            hints.append(f"High dead tuples ({dead_ratio}%) — run VACUUM")
        elif dead_ratio > 10:
            health = "warning"
            hints.append(f"Dead tuples at {dead_ratio}%")
        if seq > 50 and (idx_ratio is None or idx_ratio < 50) and total_rows > 100:
            if health == "healthy":
                health = "warning"
            hints.append("High seq scan rate — consider an index")
        if cache_hit is not None and cache_hit < 90:
            if health == "healthy":
                health = "warning"
            hints.append(f"Low cache hit ratio ({cache_hit}%)")
        rows.append({
            "table_name": d["table_name"],
            "total_bytes": d["total_bytes"] or 0,
            "table_bytes": d["table_bytes"] or 0,
            "indexes_bytes": d["indexes_bytes"] or 0,
            "live_rows": live,
            "dead_rows": dead,
            "dead_ratio": dead_ratio,
            "seq_scan": seq,
            "idx_scan": idx,
            "idx_scan_ratio": idx_ratio,
            "cache_hit_ratio": cache_hit,
            "last_vacuum": last_vac.isoformat() if last_vac else None,
            "last_analyze": last_ana.isoformat() if last_ana else None,
            "health": health,
            "hints": hints,
        })
    await _hset("db_tables", rows, 30)
    return rows


# ── DB index stats ─────────────────────────────────────────────────────────────

async def db_index_stats(db: AsyncSession) -> list[dict[str, Any]]:
    hit = await _hget("db_indexes")
    if hit is not None:
        return hit

    result = await db.execute(text("""
        SELECT
            i.relname AS index_name,
            t.relname AS table_name,
            pg_relation_size(ix.indexrelid) AS index_bytes,
            COALESCE(s.idx_scan, 0) AS scans,
            COALESCE(s.idx_tup_read, 0) AS rows_read,
            COALESCE(s.idx_tup_fetch, 0) AS rows_fetched,
            ix.indisunique,
            ix.indisprimary
        FROM pg_index ix
        JOIN pg_class i ON i.oid = ix.indexrelid
        JOIN pg_class t ON t.oid = ix.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = ix.indexrelid
        WHERE n.nspname = 'public'
        ORDER BY COALESCE(s.idx_scan, 0) ASC, pg_relation_size(ix.indexrelid) DESC
    """))
    rows = []
    for r in result.mappings().all():
        d = dict(r)
        scans = d["scans"]
        size = d["index_bytes"] or 0
        is_pk = bool(d.get("indisprimary"))
        is_uniq = bool(d.get("indisunique"))
        health, hint = "healthy", None
        if scans == 0 and not is_pk and not is_uniq and size > 8192:
            health = "unused"
            hint = "Never scanned — consider dropping"
        elif scans < 10 and not is_pk and not is_uniq and size > 1_048_576:
            health = "warning"
            hint = f"Rarely scanned ({scans}×)"
        rows.append({
            "index_name": d["index_name"],
            "table_name": d["table_name"],
            "index_bytes": size,
            "scans": scans,
            "rows_read": d["rows_read"],
            "rows_fetched": d["rows_fetched"],
            "is_primary": is_pk,
            "is_unique": is_uniq,
            "health": health,
            "hint": hint,
        })
    await _hset("db_indexes", rows, 60)
    return rows


# ── DB live activity ───────────────────────────────────────────────────────────

async def db_live_activity(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(text("""
        SELECT
            pid,
            state,
            wait_event_type,
            wait_event,
            LEFT(query, 500) AS query,
            query_start,
            client_addr::text AS client_addr,
            application_name,
            EXTRACT(EPOCH FROM (now() - query_start))::int AS duration_s
        FROM pg_stat_activity
        WHERE state IS NOT NULL
          AND state != 'idle'
          AND query NOT ILIKE '%pg_stat_activity%'
          AND backend_type = 'client backend'
        ORDER BY query_start ASC NULLS LAST
        LIMIT 30
    """))
    rows = []
    for r in result.mappings().all():
        d = dict(r)
        rows.append({
            "pid": d["pid"],
            "state": d["state"] or "",
            "wait_event_type": d.get("wait_event_type"),
            "wait_event": d.get("wait_event"),
            "query": d.get("query") or "",
            "query_start": d["query_start"].isoformat() if d.get("query_start") else None,
            "duration_s": int(d.get("duration_s") or 0),
            "client_addr": d.get("client_addr"),
            "application_name": d.get("application_name") or "",
        })
    return rows


# ── DB cache stats ─────────────────────────────────────────────────────────────

async def db_cache_stats(db: AsyncSession) -> dict[str, Any]:
    hit = await _hget("db_cache")
    if hit is not None:
        return hit

    agg = await db.execute(text("""
        SELECT
            SUM(heap_blks_read)  AS total_read,
            SUM(heap_blks_hit)   AS total_hit,
            CASE WHEN SUM(heap_blks_read + heap_blks_hit) > 0
                 THEN ROUND(100.0 * SUM(heap_blks_hit) / SUM(heap_blks_read + heap_blks_hit), 2)
                 ELSE NULL END AS hit_ratio,
            SUM(idx_blks_read)   AS idx_read,
            SUM(idx_blks_hit)    AS idx_hit,
            CASE WHEN SUM(idx_blks_read + idx_blks_hit) > 0
                 THEN ROUND(100.0 * SUM(idx_blks_hit) / SUM(idx_blks_read + idx_blks_hit), 2)
                 ELSE NULL END AS idx_hit_ratio
        FROM pg_statio_user_tables
    """))
    row = dict(agg.mappings().first() or {})
    worst = await db.execute(text("""
        SELECT relname AS table_name, heap_blks_read, heap_blks_hit,
               CASE WHEN (heap_blks_read + heap_blks_hit) > 0
                    THEN ROUND(100.0 * heap_blks_hit / (heap_blks_read + heap_blks_hit), 2)
                    ELSE NULL END AS hit_ratio
        FROM pg_statio_user_tables
        WHERE (heap_blks_read + heap_blks_hit) > 0
        ORDER BY heap_blks_read DESC LIMIT 10
    """))
    result = {
        "table_hit_ratio": float(row["hit_ratio"]) if row.get("hit_ratio") is not None else None,
        "index_hit_ratio": float(row["idx_hit_ratio"]) if row.get("idx_hit_ratio") is not None else None,
        "total_reads": int(row.get("total_read") or 0),
        "total_hits": int(row.get("total_hit") or 0),
        "worst_tables": [
            {
                "table_name": d["table_name"],
                "reads": d.get("heap_blks_read") or 0,
                "hits": d.get("heap_blks_hit") or 0,
                "hit_ratio": float(d["hit_ratio"]) if d.get("hit_ratio") is not None else None,
            }
            for d in [dict(r) for r in worst.mappings().all()]
        ],
    }
    await _hset("db_cache", result, 30)
    return result


# ── DB locks ───────────────────────────────────────────────────────────────────

async def db_locks(db: AsyncSession) -> list[dict[str, Any]]:
    result = await db.execute(text("""
        SELECT
            blocked.pid AS blocked_pid,
            LEFT(blocked_a.query, 300) AS blocked_query,
            blocking.pid AS blocking_pid,
            LEFT(blocking_a.query, 300) AS blocking_query,
            EXTRACT(EPOCH FROM now() - blocked_a.query_start)::int AS wait_s
        FROM pg_locks blocked
        JOIN pg_stat_activity blocked_a ON blocked_a.pid = blocked.pid
        JOIN pg_locks blocking
            ON blocking.locktype = blocked.locktype
           AND blocking.database IS NOT DISTINCT FROM blocked.database
           AND blocking.relation IS NOT DISTINCT FROM blocked.relation
           AND blocking.pid != blocked.pid
        JOIN pg_stat_activity blocking_a ON blocking_a.pid = blocking.pid
        WHERE NOT blocked.granted
        ORDER BY wait_s DESC LIMIT 20
    """))
    return [
        {
            "blocked_pid": d["blocked_pid"],
            "blocked_query": d.get("blocked_query") or "",
            "blocking_pid": d["blocking_pid"],
            "blocking_query": d.get("blocking_query") or "",
            "wait_s": int(d.get("wait_s") or 0),
        }
        for d in [dict(r) for r in result.mappings().all()]
    ]


async def explain_query(db: AsyncSession, query: str) -> dict[str, Any]:
    normalized = re.sub(r'\$\d+', 'NULL', query.strip())
    if not normalized:
        return {"plan": None, "error": "Empty query"}
    try:
        result = await db.execute(text(f"EXPLAIN {normalized}"))
        lines = [row[0] for row in result.fetchall()]
        return {"plan": "\n".join(lines), "error": None}
    except Exception as e:
        return {"plan": None, "error": str(e)}
