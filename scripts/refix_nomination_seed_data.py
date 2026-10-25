"""One-off fix for seed data created before nomination forms had real
fields: wipes existing nomination forms/fields/submissions and re-seeds them
via seeders.domains.nominations, which now creates a real NominationFormField
and keys each submission's answers off its field_id instead of an arbitrary
string. Safe to re-run; only ever touches whatever DATABASE_URL points at.

    docker compose exec app python -m scripts.refix_nomination_seed_data
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.base import async_session_maker
from app.models.election import Election
from app.models.nomination import NominationForm, NominationFormField, NominationSubmission
from seeders.domains.nominations import seed_nominations
from seeders.safety import UnsafeSeedTarget, assert_safe_to_seed


async def main() -> None:
    try:
        assert_safe_to_seed(settings.DATABASE_URL)
    except UnsafeSeedTarget as exc:
        print(f"ABORTED: {exc}")
        sys.exit(1)

    async with async_session_maker() as db:
        await db.execute(delete(NominationSubmission))
        await db.execute(delete(NominationFormField))
        await db.execute(delete(NominationForm))
        await db.commit()

        elections = (
            (await db.execute(select(Election).options(selectinload(Election.categories))))
            .scalars()
            .all()
        )
        await seed_nominations(db, list(elections))
        await db.commit()

    print("Re-seeded nomination forms + submissions with real fields.")


if __name__ == "__main__":
    asyncio.run(main())
