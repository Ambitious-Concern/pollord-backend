"""
One-off backfill: organization members added as editor or member before
acceptance granted platform roles hold none of them, so the organizer app —
which gates its dashboard on Election Administrator / Event Organizer —
shows them nothing. They can sign in and still not see the organization they
were invited to.

Idempotent: only members missing a role are touched, so it is safe to re-run.

Run inside the backend container (has the same DATABASE_URL as the app):
    docker compose exec app python scripts/backfill_member_platform_roles.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.base import async_session_maker
from app.services.org_role_backfill import backfill_member_platform_roles


async def main():
    async with async_session_maker() as db:
        changed = await backfill_member_platform_roles(db)
        for user_id in changed:
            print(f"  granted member platform roles to {user_id}")
        await db.commit()
        print(f"Done. {len(changed)} member(s) updated.")


if __name__ == "__main__":
    asyncio.run(main())
