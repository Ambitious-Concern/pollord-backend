import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.election import Election
from app.models.event import Event


async def generate_unique_ussd_code(db: AsyncSession) -> str:
    """6-digit numeric code, quick to type on a feature-phone keypad. Checked
    against both elections and events — a USSD caller looks one up by code
    alone before we know which type it is, so uniqueness has to span both."""
    for _ in range(20):
        code = str(secrets.randbelow(900000) + 100000)
        election_hit = await db.execute(
            select(Election.election_id).where(Election.ussd_code == code)
        )
        if election_hit.scalar_one_or_none():
            continue
        event_hit = await db.execute(select(Event.event_id).where(Event.ussd_code == code))
        if event_hit.scalar_one_or_none():
            continue
        return code
    raise RuntimeError("Could not generate a unique USSD code after 20 attempts")
