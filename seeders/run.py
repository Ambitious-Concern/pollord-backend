"""Entrypoint for the whole seeding run — grouped by domain, idempotent
(safe to re-run; tops up rather than duplicates), real Unsplash images.

Every domain only ever ADDS rows toward its target count — none of them can
shrink an already-larger dataset. To actually reduce what's in the target
database (e.g. after lowering a count in config.py), reset the schema first:

    docker compose exec app alembic downgrade base
    docker compose exec app alembic upgrade head
    docker compose exec app python -m seeders.run

Usage (local dev DB):
    DATABASE_URL=postgresql+asyncpg://pollard:pollard@localhost:55432/pollard \
        .venv/Scripts/python.exe -m seeders.run

Or inside the backend container:
    docker compose exec app python -m seeders.run

This never touches production — it only ever writes to whatever
DATABASE_URL the environment it's run in points at. Point it at a local/dev
database; don't run it against anything you don't want filled with seed
data.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from app.db.base import async_session_maker
from app.db.init_db import init_db
from seeders import config
from seeders.data.nominee_pool import NomineePicker, build_identity_pool
from seeders.domains.activity_log import seed_activity_log
from seeders.domains.elections import seed_elections
from seeders.domains.events import seed_events
from seeders.domains.nominations import seed_nominations
from seeders.domains.organizations import seed_organizations
from seeders.domains.payouts import seed_payout_history
from seeders.domains.promo_codes import seed_promo_codes
from seeders.domains.sessions import seed_admin_login_activity, seed_admin_sessions
from seeders.domains.support import seed_live_chats, seed_support_tickets
from seeders.domains.templates import seed_templates
from seeders.domains.tickets import seed_ticket_sales
from seeders.domains.users import seed_support_agents, seed_voters
from seeders.domains.voting import seed_votes
from seeders.safety import UnsafeSeedTarget, assert_safe_to_seed


async def main() -> None:
    try:
        assert_safe_to_seed(settings.DATABASE_URL)
    except UnsafeSeedTarget as exc:
        print(f"ABORTED: {exc}")
        sys.exit(1)

    started = time.monotonic()
    print(f"Seeding: {config.ORG_NAME}, {config.N_VOTERS} voters, "
          f"{config.N_ELECTIONS} elections, {config.N_EVENTS} events...")

    await init_db()  # ensures the fixed role set exists before we grant any

    async with async_session_maker() as db:
        print(f"- {config.ORG_NAME} + team ({config.N_TEAM_MEMBERS} members, {config.N_PENDING_INVITES} invites)")
        org, admin, members = await seed_organizations(db)
        await seed_admin_sessions(db, admin)
        await seed_admin_login_activity(db, admin)
        await db.commit()

        print("- voters + support agents")
        voters = await seed_voters(db)
        agents = await seed_support_agents(db)
        await db.commit()

        nominee_picker = NomineePicker(build_identity_pool())

        print(f"- elections ({config.N_ELECTIONS})")
        elections = await seed_elections(db, org, members, nominee_picker)
        await db.commit()

        print(f"- events ({config.N_EVENTS})")
        events, revenue_event_ids = await seed_events(db, org, members, nominee_picker)
        await db.commit()

        print("- votes (free + paid) — this is the slow one")
        await seed_votes(db, elections, events, voters)
        await db.commit()

        print(f"- ticket sales ({config.N_ATTENDEES_TOTAL} attendees)")
        await seed_ticket_sales(db, events, voters, revenue_event_ids)
        await db.commit()

        print(f"- promo codes ({config.N_PROMO_CODES})")
        await seed_promo_codes(db, [org], elections, events)
        await db.commit()

        print(f"- templates ({config.N_TEMPLATES})")
        await seed_templates(db, [org], elections, events)
        await db.commit()

        print("- nomination forms + submissions")
        await seed_nominations(db, elections)
        await db.commit()

        print(f"- payout history ({config.N_PAYOUT_HISTORY})")
        revenue_elections = [e for e in elections if e.allow_revoting]
        revenue_events = [e for e in events if e.event_id in revenue_event_ids]
        await seed_payout_history(db, revenue_elections, revenue_events)
        await db.commit()

        print(f"- support tickets ({config.N_SUPPORT_EMAIL_TICKETS} email, {config.N_SUPPORT_CHATS} chat)")
        await seed_support_tickets(db, voters)
        await seed_live_chats(db, voters, agents)
        await db.commit()

        print("- org activity log")
        await seed_activity_log(db, [org])
        await db.commit()

    elapsed = time.monotonic() - started
    print(f"Done in {elapsed:.1f}s.")


if __name__ == "__main__":
    asyncio.run(main())
