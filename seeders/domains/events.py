"""config.N_EVENTS events with a global 10-ticket-type budget spread across
them, dated across past/future so ticket-sales history and upcoming
listings both have data. Some genres (tech/gala/cultural) also run
categories+candidates, drawn from the same shared nominee pool elections
use — the real "hybrid" event pattern (ticketed entry + its own voting).

Exactly config.N_PAYOUT_SOURCE_EVENTS published events are designated to
actually receive ticket sales (see seed_ticket_sales) — everything else may
still list ticket types for sale, just with zero sold so far, which is
itself realistic for an upcoming event. At these volumes (a handful of
voting-enabled events, 3-8 candidates each) event-sourced nominees land
well under config.N_NOMINEES_EVENT_CAP without needing to enforce it."""
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.slug import generate_slug
from app.models.election import Candidate, Category
from app.models.event import Event, TicketType
from app.models.organization import Organization
from app.models.user import User
from seeders import config
from seeders.config import rng
from seeders.data import images
from seeders.data.coordinates import coords_for_place
from seeders.data.copy_bank import EVENT_GENRES, PLACES, TICKET_TIERS, VENUES, unique_titles
from seeders.data.distribute import distribute
from seeders.data.nominee_pool import NomineePicker

PLAN_CYCLE = ["standard", "standard", "premium"]


def _infer_revenue_ids(events: list[Event]) -> set[UUID]:
    """Re-derives the designated revenue subset on a top-up run where
    seed_events returns early — the first N_PAYOUT_SOURCE_EVENTS published
    events carrying at least one ticket type."""
    ids: set[UUID] = set()
    for event in events:
        if event.status == "published" and event.ticket_types:
            ids.add(event.event_id)
        if len(ids) >= config.N_PAYOUT_SOURCE_EVENTS:
            break
    return ids


async def seed_events(
    db: AsyncSession, org: Organization, members: list[User], nominee_picker: NomineePicker,
) -> tuple[list[Event], set[UUID]]:
    existing_titles = {row[0] for row in (await db.execute(select(Event.title))).all()}

    # Eagerly load here and on the final return below — voting.py/
    # tickets.py iterate .categories/.ticket_types extensively later in the
    # run, and a relationship access on an AsyncSession-bound object
    # outside an explicit await is unreliable (implicit lazy loading
    # intermittently raises sqlalchemy.exc.MissingGreenlet under asyncpg).
    event_options = (
        selectinload(Event.categories).selectinload(Category.candidates),
        selectinload(Event.ticket_types),
    )

    if len(existing_titles) >= config.N_EVENTS:
        result = await db.execute(select(Event).options(*event_options))
        events = list(result.scalars().all())[: config.N_EVENTS]
        return events, _infer_revenue_ids(events)

    year = datetime.now(timezone.utc).year
    needed = config.N_EVENTS - len(existing_titles)
    picks = unique_titles(EVENT_GENRES, needed, year, set(existing_titles))

    now = datetime.now(timezone.utc)
    statuses = ["draft" if i % 7 == 0 else "published" for i in range(len(picks))]
    published_indices = [i for i, s in enumerate(statuses) if s == "published"]
    revenue_indices = set(published_indices[: config.N_PAYOUT_SOURCE_EVENTS])

    ticket_counts = distribute(config.N_TICKET_TYPES_TOTAL, len(picks), minimum=0)
    for idx in revenue_indices:
        if ticket_counts[idx] == 0:
            donor = max(range(len(ticket_counts)), key=lambda k: ticket_counts[k])
            if ticket_counts[donor] > 0:
                ticket_counts[donor] -= 1
                ticket_counts[idx] = 1

    events: list[Event] = []
    revenue_event_ids: set[UUID] = set()

    for i, (title, genre) in enumerate(picks):
        status = statuses[i]
        # Always the org owner (members[0]) — see the matching comment in
        # elections.py; the public listing page's organizer lookup 404s for
        # anything created by a non-owner teammate.
        creator = members[0]

        offset_days = rng.randint(-config.DATA_SPREAD_DAYS, 60)
        event_date = (now + timedelta(days=offset_days)).date()
        plan = PLAN_CYCLE[i % len(PLAN_CYCLE)]
        place = rng.choice(PLACES)
        lat, lng = coords_for_place(place)
        venue = rng.choice(VENUES)

        event = Event(
            title=title,
            slug=f"{generate_slug(title)}-{i}",
            description=f"{title} — a ticketed event on Pollord.",
            event_date=event_date,
            event_time=time(rng.choice([9, 10, 14, 17, 18, 19]), rng.choice([0, 30])),
            location=venue,
            latitude=lat,
            longitude=lng,
            category=genre["tag"],
            capacity=rng.choice([None, 200, 500, 1000, 2000]),
            banner_image_url=images.event_banner(i + 100),
            status=status,
            created_by=creator.user_id,
            org_id=org.org_id,
            plan=plan,
            # Paid event-side voting is deliberately left off the seed data —
            # the paid-vote transaction budget (see voting.py) is confined
            # to the designated elections so the platform-wide 600-
            # transaction total stays exactly controllable.
            vote_price=None,
        )
        db.add(event)
        await db.flush()

        for tier_name, tier_desc, price_min, price_max in TICKET_TIERS[: ticket_counts[i]]:
            db.add(TicketType(
                event_id=event.event_id,
                type_name=tier_name,
                description=tier_desc,
                price=Decimal(rng.randint(price_min, price_max)),
                quantity_available=rng.randint(50, 500),
                max_per_user=rng.choice([4, 5, 6, 10]),
            ))

        if i in revenue_indices:
            revenue_event_ids.add(event.event_id)

        if genre["voting_categories"]:
            for c_order, cat_name in enumerate(genre["voting_categories"]):
                category = Category(
                    event_id=event.event_id,
                    name=cat_name,
                    election_type="single_choice",
                    display_order=c_order,
                )
                db.add(category)
                await db.flush()

                n_candidates = rng.randint(config.MIN_CANDIDATES_PER_CATEGORY, config.MAX_CANDIDATES_PER_CATEGORY)
                for cand_order in range(n_candidates):
                    identity = nominee_picker.next()
                    db.add(Candidate(
                        category_id=category.category_id,
                        event_id=event.event_id,
                        name=identity.name,
                        short_code=f"E{i:02d}{c_order}{cand_order}",
                        image_url=identity.image_url if cand_order % 3 == 0 else None,
                        display_order=cand_order,
                    ))

        await db.flush()
        events.append(event)

    event_ids = [e.event_id for e in events]
    result = await db.execute(select(Event).options(*event_options).where(Event.event_id.in_(event_ids)))
    return list(result.scalars().all()), revenue_event_ids
