"""Saved templates snapshotted from real seeded elections/events — mix of
private and public (the one cross-org-visible resource in the app), with a
genuinely descriptive summary of what each template actually configures
rather than a one-line placeholder."""
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.election import Election
from app.models.event import Event
from app.models.organization import Organization
from app.models.template import Template
from seeders import config
from seeders.config import rng


def _election_description(source: Election, categories: list[str]) -> str:
    if categories:
        cat_list = ", ".join(categories)
        cat_summary = f"{len(categories)} categor{'y' if len(categories) == 1 else 'ies'} ({cat_list})"
    else:
        cat_summary = "no categories configured yet"
    price = getattr(source, "vote_price", None)
    pricing = f"GH₵{price / 100:.2f} per paid vote" if price else "free voting"
    verification = "requires voter verification" if source.require_verification else "no extra verification step"
    return (
        f"A {source.tag or 'voting'}-style setup modeled on \"{source.title}\": {cat_summary}, "
        f"{pricing}, {verification}, and {source.visibility} visibility by default."
    )


def _event_description(source: Event, ticket_tiers: list[dict]) -> str:
    if ticket_tiers:
        tiers = ", ".join(f"{t['name']} (GH₵{Decimal(t['price']):.2f})" for t in ticket_tiers)
        pricing = f"{len(ticket_tiers)} ticket tier{'s' if len(ticket_tiers) != 1 else ''} — {tiers}"
    else:
        pricing = "no ticket tiers configured yet"
    return (
        f"A {source.category or 'ticketed'}-style setup modeled on \"{source.title}\": {pricing}, "
        f"at {source.location or 'a venue to be confirmed'}."
    )


async def seed_templates(
    db: AsyncSession, orgs: list[Organization], elections: list[Election], events: list[Event],
) -> None:
    existing_count = (await db.execute(select(func.count()).select_from(Template))).scalar_one()
    if existing_count >= config.N_TEMPLATES:
        return

    for i in range(existing_count, config.N_TEMPLATES):
        org = orgs[i % len(orgs)]
        is_election = i % 2 == 0 and elections
        source = rng.choice(elections) if is_election else rng.choice(events)

        categories = [c.name for c in source.categories] if is_election else None
        ticket_tiers = None
        voting_type = None
        vote_price_pesewas = None

        if not is_election:
            ticket_tiers = [
                {"name": t.type_name, "price": str(t.price), "description": t.description or "", "available": t.quantity_available}
                for t in source.ticket_types
            ] or None

        if is_election:
            vote_price_pesewas = getattr(source, "vote_price", None)
            voting_type = "paid" if vote_price_pesewas else "free"
            description = _election_description(source, categories or [])
        else:
            description = _event_description(source, ticket_tiers or [])

        db.add(Template(
            org_id=org.org_id,
            created_by=org.owner_id,
            kind="election" if is_election else "event",
            title=f"{source.title} Template",
            description=description,
            visibility="public" if i % 3 == 0 else "private",
            categories=categories,
            ticket_tiers=ticket_tiers,
            voting_type=voting_type,
            vote_price_pesewas=vote_price_pesewas,
        ))

    await db.flush()
