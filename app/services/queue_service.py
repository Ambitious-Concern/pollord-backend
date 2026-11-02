"""Queue management: per-queue stats, job listing by state, retry, pause/resume."""
import asyncio
import base64
import json
from typing import Any

from app.core.redis import get_redis

NAMED_QUEUES = ["celery", "emails", "reports"]


async def queue_cards() -> list[dict[str, Any]]:
    """Return Waiting/Active/Completed/Failed counts per named queue."""
    r = await get_redis()

    def _inspect_active():
        try:
            from app.tasks.celery_app import celery_app
            return celery_app.control.inspect(timeout=2).active() or {}
        except Exception:
            return {}

    loop = asyncio.get_event_loop()
    active_by_worker = await loop.run_in_executor(None, _inspect_active)

    active_by_queue: dict[str, int] = {}
    for tasks in active_by_worker.values():
        for t in tasks:
            q = (t.get("delivery_info") or {}).get("routing_key") or "celery"
            active_by_queue[q] = active_by_queue.get(q, 0) + 1

    cards = []
    for q in NAMED_QUEUES:
        waiting = await r.llen(q)
        completed = int(await r.get(f"celery:stats:{q}:completed") or 0)
        failed = int(await r.get(f"celery:stats:{q}:failed") or 0)
        cards.append({
            "name": q,
            "waiting": waiting,
            "active": active_by_queue.get(q, 0),
            "completed": completed,
            "failed": failed,
        })
    return cards


async def list_jobs(state: str = "FAILURE", queue: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    state = state.upper()
    r = await get_redis()
    if state == "ACTIVE":
        return await _active_jobs(queue)
    if state == "WAITING":
        return await _waiting_jobs(queue, limit, r)
    if state in ("FAILURE", "SUCCESS"):
        return await _indexed_jobs(state, queue, limit, r)
    return []


async def _active_jobs(queue: str | None) -> list[dict[str, Any]]:
    def _inspect():
        try:
            from app.tasks.celery_app import celery_app
            return celery_app.control.inspect(timeout=2).active() or {}
        except Exception:
            return {}

    loop = asyncio.get_event_loop()
    active = await loop.run_in_executor(None, _inspect)
    jobs = []
    for tasks in active.values():
        for t in tasks:
            q = (t.get("delivery_info") or {}).get("routing_key") or "celery"
            if queue and q != queue:
                continue
            name = t.get("name", "")
            jobs.append({
                "task_id": t.get("id"),
                "task_name": name,
                "short_name": name.rsplit(".", 1)[-1],
                "queue": q,
                "status": "ACTIVE",
                "started_at": str(t.get("time_start")) if t.get("time_start") else None,
                "date_done": None,
                "args_preview": str(t.get("args", []))[:120],
                "error": None,
                "traceback": None,
            })
    return jobs


async def _waiting_jobs(queue: str | None, limit: int, r: Any) -> list[dict[str, Any]]:
    queues = [queue] if queue else NAMED_QUEUES
    jobs = []
    for q in queues:
        msgs = await r.lrange(q, 0, limit - 1)
        for raw in msgs:
            try:
                msg = json.loads(raw)
                # Celery v2 (default since v4): task name + id are in headers.
                # v1 fallback: body is a JSON dict with "task"/"id" keys.
                headers = msg.get("headers") or {}
                name = headers.get("task", "")
                tid = headers.get("id", "")
                args_preview = str(headers.get("argsrepr", "[]"))[:120]

                # v1 fallback: body is a JSON dict (not the v2 list)
                if not name or not tid:
                    body_raw = msg.get("body", "")
                    if isinstance(body_raw, str):
                        try:
                            decoded = json.loads(base64.b64decode(body_raw + "=="))
                            if isinstance(decoded, dict):
                                name = name or decoded.get("task", "")
                                tid = tid or decoded.get("id", "")
                                args_preview = str(decoded.get("args", []))[:120]
                        except Exception:
                            pass

                if not tid:
                    continue
                jobs.append({
                    "task_id": tid,
                    "task_name": name,
                    "short_name": name.rsplit(".", 1)[-1],
                    "queue": q,
                    "status": "WAITING",
                    "started_at": None,
                    "date_done": None,
                    "args_preview": args_preview,
                    "error": None,
                    "traceback": None,
                })
            except Exception:
                pass
    return jobs[:limit]


async def _indexed_jobs(status: str, queue: str | None, limit: int, r: Any) -> list[dict[str, Any]]:
    if status == "FAILURE":
        raw_ids = await r.zrevrange("celery:failed_jobs", 0, limit * 3 - 1)
    else:
        raw_ids = []
        src_queues = [queue] if queue else NAMED_QUEUES
        for q in src_queues:
            raw_ids += await r.zrevrange(f"celery:completed_jobs:{q}", 0, 49)

    jobs = []
    for tid_raw in raw_ids:
        tid = tid_raw.decode() if isinstance(tid_raw, bytes) else tid_raw
        meta_raw = await r.hgetall(f"celery:job:{tid}")
        if not meta_raw:
            continue
        meta = {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in meta_raw.items()
        }
        job_queue = meta.get("queue", "celery")
        if queue and job_queue != queue:
            continue
        name = meta.get("task_name", "")
        jobs.append({
            "task_id": tid,
            "task_name": name,
            "short_name": name.rsplit(".", 1)[-1],
            "queue": job_queue,
            "status": status,
            "started_at": meta.get("started_at"),
            "date_done": meta.get("failed_at") if status == "FAILURE" else None,
            "args_preview": meta.get("args", "[]")[:120],
            "error": meta.get("error"),
            "traceback": meta.get("traceback"),
        })
        if len(jobs) >= limit:
            break
    return jobs


async def retry_job(task_id: str) -> dict[str, Any]:
    r = await get_redis()
    meta_raw = await r.hgetall(f"celery:job:{task_id}")
    if not meta_raw:
        return {"success": False, "error": "Metadata expired or task not tracked"}

    def _d(v: Any) -> str:
        return v.decode() if isinstance(v, bytes) else str(v)

    meta = {_d(k): _d(v) for k, v in meta_raw.items()}
    task_name = meta.get("task_name", "")
    if not task_name:
        return {"success": False, "error": "task_name not stored"}

    try:
        args = json.loads(meta.get("args", "[]"))
        kwargs = json.loads(meta.get("kwargs", "{}"))
        queue = meta.get("queue", "celery")
    except Exception:
        args, kwargs, queue = [], {}, "celery"

    from app.tasks.celery_app import celery_app
    new_task = celery_app.send_task(task_name, args=args, kwargs=kwargs, queue=queue)
    return {"success": True, "new_task_id": str(new_task.id)}


async def purge_failed(queue: str | None) -> dict[str, Any]:
    """Delete all tracked failed jobs for a specific queue (or every queue if None)."""
    r = await get_redis()
    raw_ids = await r.zrange("celery:failed_jobs", 0, -1)

    to_remove: list[str] = []
    for tid_raw in raw_ids:
        tid = tid_raw.decode() if isinstance(tid_raw, bytes) else tid_raw
        if queue:
            job_queue = await r.hget(f"celery:job:{tid}", "queue")
            if isinstance(job_queue, bytes):
                job_queue = job_queue.decode()
            if (job_queue or "celery") != queue:
                continue
        to_remove.append(tid)

    if to_remove:
        pipe = r.pipeline(transaction=False)
        for tid in to_remove:
            pipe.delete(f"celery:job:{tid}")
            pipe.zrem("celery:failed_jobs", tid)
        if queue:
            pipe.set(f"celery:stats:{queue}:failed", 0)
        else:
            for q in NAMED_QUEUES:
                pipe.set(f"celery:stats:{q}:failed", 0)
        await pipe.execute()

    return {"success": True, "removed": len(to_remove)}


async def pause_queue(queue: str) -> dict[str, Any]:
    try:
        from app.tasks.celery_app import celery_app
        def _pause():
            celery_app.control.cancel_consumer(queue, reply=True, timeout=2)
        await asyncio.get_event_loop().run_in_executor(None, _pause)
        return {"success": True}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def resume_queue(queue: str) -> dict[str, Any]:
    try:
        from app.tasks.celery_app import celery_app
        def _resume():
            celery_app.control.add_consumer(queue, reply=True, timeout=2)
        await asyncio.get_event_loop().run_in_executor(None, _resume)
        return {"success": True}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
