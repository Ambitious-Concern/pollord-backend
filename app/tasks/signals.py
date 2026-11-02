"""Celery task signals — track job metadata and stats in Redis for queue management UI."""
import json
import logging
import time
from datetime import datetime, timezone

from celery.signals import task_failure, task_postrun, task_prerun

logger = logging.getLogger(__name__)


def _sync_redis():
    import redis as redis_lib
    from app.core.config import settings
    return redis_lib.Redis.from_url(settings.REDIS_URL, decode_responses=True)


@task_prerun.connect
def on_task_prerun(task_id, task, args, kwargs, **_):
    try:
        r = _sync_redis()
        queue = (getattr(task.request, "delivery_info", {}) or {}).get("routing_key") or "celery"
        r.hset(f"celery:job:{task_id}", mapping={
            "task_name": task.name,
            "queue": queue,
            "args": json.dumps(list(args or []), default=str)[:500],
            "kwargs": json.dumps(dict(kwargs or {}), default=str)[:500],
            "started_at": datetime.now(timezone.utc).isoformat(),
        })
        r.expire(f"celery:job:{task_id}", 86400 * 7)
    except Exception as exc:
        logger.debug("task_prerun signal: %s", exc)


@task_postrun.connect
def on_task_postrun(task_id, task, retval, state, **_):
    try:
        r = _sync_redis()
        queue = r.hget(f"celery:job:{task_id}", "queue") or "celery"
        if state == "SUCCESS":
            r.incr(f"celery:stats:{queue}:completed")
            r.zadd(f"celery:completed_jobs:{queue}", {task_id: time.time()})
            r.zremrangebyrank(f"celery:completed_jobs:{queue}", 0, -501)
    except Exception as exc:
        logger.debug("task_postrun signal: %s", exc)


@task_failure.connect
def on_task_failure(task_id, exception, traceback, **_):
    try:
        r = _sync_redis()
        queue = r.hget(f"celery:job:{task_id}", "queue") or "celery"
        r.incr(f"celery:stats:{queue}:failed")
        r.zadd("celery:failed_jobs", {task_id: time.time()})
        r.zremrangebyrank("celery:failed_jobs", 0, -501)
        r.hset(f"celery:job:{task_id}", mapping={
            "error": str(exception)[:500],
            "traceback": str(traceback)[:3000],
            "failed_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        logger.debug("task_failure signal: %s", exc)
