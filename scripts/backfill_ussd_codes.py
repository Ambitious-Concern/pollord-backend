"""
One-off backfill: the ussd_code column (migration d4e8b2f6a1c9) was added
nullable with no data backfill, so every election/event that existed before
that migration has ussd_code = NULL and can never be dialed by voters, even
though the webhook and generation logic both work correctly for new rows.

Idempotent — only touches rows where ussd_code IS NULL.

Run inside the backend container (has the same DATABASE_URL as the app):
    docker compose exec app python scripts/backfill_ussd_codes.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.core.ussd import generate_unique_ussd_code
from app.db.base import async_session_maker
from app.models.election import Election
from app.models.event import Event


async def main():
    async with async_session_maker() as db:
        elections = (
            await db.execute(select(Election).where(Election.ussd_code.is_(None)))
        ).scalars().all()
        events = (
            await db.execute(select(Event).where(Event.ussd_code.is_(None)))
        ).scalars().all()

        for election in elections:
            election.ussd_code = await generate_unique_ussd_code(db)
            print(f"  election {election.slug}: {election.ussd_code}")

        for event in events:
            event.ussd_code = await generate_unique_ussd_code(db)
            print(f"  event {event.slug}: {event.ussd_code}")

        await db.commit()
        print(f"Done. Backfilled {len(elections)} election(s), {len(events)} event(s).")


if __name__ == "__main__":
    asyncio.run(main())
