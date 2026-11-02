from celery import Celery
from celery.schedules import crontab

from app.core.config import settings
from app.tasks import signals as _signals  # noqa: F401

celery_app = Celery(
    "pollard",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[
        "app.tasks.email_tasks",
        "app.tasks.vote_tally_tasks",
        "app.tasks.ticket_tasks",
        "app.tasks.auth_tasks",
        "app.tasks.support_chat_tasks",
        "app.tasks.report_export_tasks",
        "app.tasks.backup_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,
    worker_max_tasks_per_child=1000,
)

celery_app.conf.task_routes = {
    "app.tasks.email_tasks.*": {"queue": "emails"},
    "app.tasks.event_broadcast_tasks.*": {"queue": "emails"},
    "app.tasks.report_export_tasks.*": {"queue": "reports"},
}

celery_app.conf.beat_schedule = {
    "check-election-status": {
        "task": "app.tasks.vote_tally_tasks.check_election_status",
        "schedule": 60.0,
    },
    "activate-scheduled-elections": {
        "task": "app.tasks.vote_tally_tasks.activate_scheduled_elections",
        "schedule": 60.0,
    },
    "check-expired-tickets": {
        "task": "app.tasks.ticket_tasks.check_expired_tickets",
        "schedule": 3600.0,
    },
    "prune-expired-refresh-tokens": {
        "task": "app.tasks.auth_tasks.prune_expired_refresh_tokens",
        "schedule": 86400.0,
    },
    "purge-closed-chats": {
        "task": "app.tasks.support_chat_tasks.purge_closed_chats",
        "schedule": 86400.0,
    },
    "backup-database": {
        "task": "app.tasks.backup_tasks.backup_database",
        # 2 AM UTC daily — quiet period between election activity peaks.
        "schedule": crontab(hour=2, minute=0),
    },
    "reap-stale-backups": {
        "task": "app.tasks.backup_tasks.reap_stale_backups",
        "schedule": 900.0,  # every 15 min
    },
}
