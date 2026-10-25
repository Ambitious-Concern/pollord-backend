"""Nomination forms + a review-queue's worth of submissions for a subset of
seeded elections — a fixed config.N_NOMINATIONS_TOTAL budget spread across
those elections' categories, enough to page through the review queue and
see every status (pending/approved/rejected) and source (public/private/
manual)."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.slug import generate_slug
from app.models.election import Election
from app.models.nomination import NominationForm, NominationFormField, NominationSubmission
from seeders import config
from seeders.config import rng
from seeders.data.distribute import distribute
from seeders.data.names import unique_full_name

_SOURCES = ["public"] * 6 + ["private"] * 2 + ["manual"] * 2
_STATUSES = ["pending"] * 5 + ["approved"] * 3 + ["rejected"] * 2


async def seed_nominations(db: AsyncSession, elections: list[Election]) -> None:
    form_elections = [e for e in elections if e.categories][: config.N_ELECTIONS_WITH_FORMS]

    forms: list[NominationForm] = []
    form_categories: list = []
    seen_names: set = set()

    for election in form_elections:
        slug = f"{generate_slug(election.title)}-nominate"
        existing = (await db.execute(select(NominationForm).where(NominationForm.slug == slug))).scalar_one_or_none()
        if existing:
            continue

        form = NominationForm(
            election_id=election.election_id,
            title=f"Nominate for {election.title}",
            description="Tell us who deserves a nomination and why.",
            visibility="public",
            slug=slug,
            created_by=election.created_by,
        )
        db.add(form)
        await db.flush()

        # A real form field, keyed the same way a real public submission
        # would be — answers below key off field.field_id, not a made-up
        # string, so the reviewer's "view submission" screen resolves a real
        # question label instead of finding nothing.
        why_field = NominationFormField(
            form_id=form.form_id,
            field_type="paragraph",
            label="Why should this person be nominated?",
            required=True,
            display_order=0,
        )
        db.add(why_field)
        await db.flush()

        forms.append(form)
        for category in election.categories:
            form_categories.append((election, form, why_field, category))

    if not form_categories:
        return

    counts = distribute(config.N_NOMINATIONS_TOTAL, len(form_categories), minimum=1)
    now = datetime.now(timezone.utc)

    for (election, form, why_field, category), n in zip(form_categories, counts):
        for _ in range(n):
            nominee_name = unique_full_name(seen_names)
            source = rng.choice(_SOURCES)
            db.add(NominationSubmission(
                form_id=form.form_id if source != "manual" else None,
                election_id=election.election_id,
                category_id=category.category_id,
                nominee_name=nominee_name,
                source=source,
                answers={str(why_field.field_id): f"{nominee_name} has been outstanding this year."}
                if source != "manual" else {},
                status=rng.choice(_STATUSES),
                submitted_at=now - timedelta(days=rng.randint(0, 60)),
            ))

    await db.flush()
