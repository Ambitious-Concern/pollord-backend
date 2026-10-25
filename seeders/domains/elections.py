"""config.N_ELECTIONS elections, spanning every status/plan/visibility
combination the dashboard needs to filter on, with a global 24-category
budget spread unevenly across them and candidates drawn from a shared,
reusable nominee pool. Idempotent by title."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.slug import generate_slug
from app.models.election import Candidate, Category, Election
from app.models.organization import Organization
from app.models.user import User
from seeders import config
from seeders.config import rng
from seeders.data import images
from seeders.data.coordinates import coords_for_venue
from seeders.data.copy_bank import ELECTION_GENRES, VENUES, unique_titles
from seeders.data.distribute import distribute
from seeders.data.nominee_pool import NomineePicker

STATUS_CYCLE = ["active", "completed", "scheduled", "draft"]
PLAN_CYCLE = ["standard", "standard", "premium"]  # ~1/3 premium, matching the mock's own ratio


def _window_for_status(status: str, now: datetime) -> tuple[datetime, datetime]:
    if status == "active":
        return now - timedelta(days=rng.randint(1, 10)), now + timedelta(days=rng.randint(3, 20))
    if status == "completed":
        end = now - timedelta(days=rng.randint(5, config.DATA_SPREAD_DAYS))
        return end - timedelta(days=rng.randint(7, 21)), end
    if status == "scheduled":
        start = now + timedelta(days=rng.randint(5, 45))
        return start, start + timedelta(days=rng.randint(3, 14))
    # draft — window doesn't matter much, but must still be well-formed
    start = now + timedelta(days=rng.randint(10, 60))
    return start, start + timedelta(days=7)


async def seed_elections(
    db: AsyncSession, org: Organization, members: list[User], nominee_picker: NomineePicker,
) -> list[Election]:
    existing_titles = {row[0] for row in (await db.execute(select(Election.title))).all()}

    election_options = (selectinload(Election.categories).selectinload(Category.candidates),)

    if len(existing_titles) >= config.N_ELECTIONS:
        result = await db.execute(select(Election).options(*election_options))
        return list(result.scalars().all())[: config.N_ELECTIONS]

    year = datetime.now(timezone.utc).year
    needed = config.N_ELECTIONS - len(existing_titles)
    picks = unique_titles(ELECTION_GENRES, needed, year, set(existing_titles))

    now = datetime.now(timezone.utc)
    statuses = [STATUS_CYCLE[i % len(STATUS_CYCLE)] for i in range(len(picks))]
    # Only active/completed elections ever get votes (see voting.py's parent
    # filter) — restrict the designated payout-source subset to those so
    # GET /payouts/sources actually returns exactly N_PAYOUT_SOURCE_ELECTIONS.
    revenue_eligible = [i for i, s in enumerate(statuses) if s in ("active", "completed")]
    revenue_indices = set(revenue_eligible[: config.N_PAYOUT_SOURCE_ELECTIONS])
    category_counts = distribute(config.N_CATEGORIES_TOTAL, len(picks), minimum=1)

    elections: list[Election] = []
    for i, (title, genre) in enumerate(picks):
        status = statuses[i]
        start, end = _window_for_status(status, now)
        plan = PLAN_CYCLE[i % len(PLAN_CYCLE)]
        # Always the org owner (members[0]), not a round-robin across the
        # team — the public listing page resolves a listing's organizer via
        # GET /organizations/owner/{created_by}, which 404s for anyone who
        # isn't literally an Organization.owner_id (team membership doesn't
        # count). Spreading authorship across teammates broke every public
        # listing not created by the owner.
        creator = members[0]
        venue = rng.choice(VENUES)
        lat, lng = coords_for_venue(venue)
        is_revenue_source = i in revenue_indices

        election = Election(
            title=title,
            slug=f"{generate_slug(title)}-{i}",
            description=f"{title} — decided entirely by public vote, with results verified live on Pollord.",
            start_datetime=start,
            end_datetime=end,
            status=status,
            created_by=creator.user_id,
            org_id=org.org_id,
            plan=plan,
            banner_image_url=images.event_banner(i),
            visibility="private" if i % 9 == 0 else "public",
            access_code=f"VOTE{1000 + i}" if i % 9 == 0 else None,
            require_verification=i % 6 == 0,
            # Only the designated payout-source subset charges for votes —
            # every other election stays free so it can never accrue revenue.
            allow_revoting=is_revenue_source,
            vote_price=rng.choice([100, 150, 200, 300]) if is_revenue_source else None,
            venue=venue,
            latitude=lat,
            longitude=lng,
            tag=genre["tag"],
        )
        db.add(election)
        await db.flush()

        n_categories = category_counts[i]
        category_names = genre["categories"]
        for c_order in range(n_categories):
            category = Category(
                election_id=election.election_id,
                name=category_names[c_order % len(category_names)],
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
                    election_id=election.election_id,
                    name=identity.name,
                    short_code=f"{i:02d}{c_order}{cand_order}",
                    image_url=identity.image_url if cand_order % 3 == 0 else None,
                    display_order=cand_order,
                ))

        await db.flush()
        elections.append(election)

    # Eagerly load categories+candidates here — voting.py/nominations.py
    # iterate them extensively after this session's next commit, and a
    # relationship access on an AsyncSession-bound object outside an
    # explicit await is unreliable (implicit lazy loading intermittently
    # raises sqlalchemy.exc.MissingGreenlet under the asyncpg driver).
    result = await db.execute(select(Election).options(*election_options))
    return list(result.scalars().all())
