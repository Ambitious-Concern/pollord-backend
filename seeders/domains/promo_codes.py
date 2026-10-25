"""Promo codes across active/expired/inactive states, mostly unscoped
(applies to everything) with some scoped to a specific election/event, plus
a handful of real redemptions so uses_count has something to show."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.election import Election
from app.models.event import Event
from app.models.organization import Organization
from app.models.promo_code import PromoCode, PromoCodeRedemption, PromoCodeScope
from seeders import config
from seeders.config import rng

_PREFIXES = ["SAVE", "EARLY", "VIP", "WELCOME", "LOYAL", "BONUS", "FLASH", "GROUP", "STUDENT", "FAM"]


async def seed_promo_codes(
    db: AsyncSession, orgs: list[Organization], elections: list[Election], events: list[Event]
) -> None:
    existing_codes = {row[0] for row in (await db.execute(select(PromoCode.code))).all()}
    if len(existing_codes) >= config.N_PROMO_CODES:
        return

    today = date.today()
    needed = config.N_PROMO_CODES - len(existing_codes)
    created = 0
    attempts = 0

    while created < needed and attempts < needed * 20:
        attempts += 1
        code = f"{rng.choice(_PREFIXES)}{rng.randint(10, 999)}"
        if code in existing_codes:
            continue
        existing_codes.add(code)

        org = rng.choice(orgs)
        discount_type = rng.choice(["percentage", "fixed_amount"])
        discount_value = Decimal(rng.choice([5, 10, 15, 20, 25])) if discount_type == "percentage" else Decimal(rng.choice([5, 10, 20, 50]))

        state = rng.choice(["active", "active", "active", "expired", "inactive"])
        if state == "expired":
            valid_from = today - timedelta(days=rng.randint(60, 120))
            valid_until = today - timedelta(days=rng.randint(1, 30))
            is_active = True
        elif state == "inactive":
            valid_from = today - timedelta(days=rng.randint(1, 30))
            valid_until = None
            is_active = False
        else:
            valid_from = today - timedelta(days=rng.randint(1, 30))
            valid_until = today + timedelta(days=rng.randint(10, 90)) if rng.random() < 0.7 else None
            is_active = True

        promo = PromoCode(
            org_id=org.org_id,
            created_by=org.owner_id,
            code=code,
            discount_type=discount_type,
            discount_value=discount_value,
            usage_limit=rng.choice([None, None, 50, 100, 200]),
            per_person_limit=rng.choice([1, 1, 2, 5]),
            valid_from=valid_from,
            valid_until=valid_until,
            is_active=is_active,
        )
        db.add(promo)
        await db.flush()

        # ~40% get a scope (election or event); the rest apply to everything.
        if rng.random() < 0.4 and (elections or events):
            if elections and (not events or rng.random() < 0.5):
                db.add(PromoCodeScope(promo_code_id=promo.promo_code_id, election_id=rng.choice(elections).election_id))
            else:
                db.add(PromoCodeScope(promo_code_id=promo.promo_code_id, event_id=rng.choice(events).event_id))

        # A few redemptions on active codes, so uses_count isn't always zero.
        if state == "active" and rng.random() < 0.5:
            for _ in range(rng.randint(1, 6)):
                db.add(PromoCodeRedemption(
                    promo_code_id=promo.promo_code_id,
                    channel=rng.choice(["ticket", "vote"]),
                    email=f"seed+voter{rng.randint(0, config.N_VOTERS - 1)}@pollord.dev",
                    discount_amount=Decimal(rng.randint(1, 20)),
                    redeemed_at=datetime.now(timezone.utc) - timedelta(days=rng.randint(0, 30)),
                ))

        created += 1

    await db.flush()
