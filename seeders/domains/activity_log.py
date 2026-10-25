"""Org activity log entries across every category the dashboard's Activity
Log page filters on, spread over time, per organization."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org_activity_log import OrgActivityLog
from app.models.organization import Organization
from seeders import config
from seeders.config import rng

_ENTRIES = [
    ("election", "created election", "{title}"),
    ("election", "updated settings for", "{title}"),
    ("event", "created event", "{title}"),
    ("event", "updated details for", "{title}"),
    ("ticketing", "added ticket type", "{title}"),
    ("voting", "approved nominee", "a nominee"),
    ("payout", "requested a payout from", "{title}"),
    ("payout", "paid out", "{title}"),
    ("front-desk", "sold a walk-up ticket for", "{title}"),
    ("team", "invited", "a new teammate"),
    ("settings", "updated", "organization settings"),
]

async def seed_activity_log(db: AsyncSession, orgs: list[Organization]) -> None:
    for org in orgs:
        existing_count = (
            await db.execute(
                select(func.count()).select_from(OrgActivityLog).where(OrgActivityLog.org_id == org.org_id)
            )
        ).scalar_one()
        if existing_count >= config.N_ACTIVITY_LOG:
            continue

        now = datetime.now(timezone.utc)
        for i in range(existing_count, config.N_ACTIVITY_LOG):
            category, verb, target_template = rng.choice(_ENTRIES)
            db.add(OrgActivityLog(
                org_id=org.org_id,
                user_id=org.owner_id,
                category=category,
                verb=verb,
                target_name=target_template.format(title=org.name),
                created_at=now - timedelta(days=rng.randint(0, 120), hours=rng.randint(0, 23)),
            ))

    await db.flush()
