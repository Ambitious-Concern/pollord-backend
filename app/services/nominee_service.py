from typing import List
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.election import Category, Election
from app.models.event import Event
from app.models.vote import Vote
from app.schemas.nominee import NomineeResponse
from app.services.cryptography_service import CryptographyService
from app.services.voting_service import tally_votes_by_category


class NomineeService:
    """Every candidate across every one of the caller's elections and
    events, with real vote counts and category rank — one decrypt-and-tally
    pass over every relevant vote, not one live-results-style call per
    election/event (which is what a naive per-source loop would cost)."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_nominees(self, teammate_ids: List[UUID]) -> List[NomineeResponse]:
        election_rows = (
            await self.db.execute(
                select(Election.election_id, Election.title).where(Election.created_by.in_(teammate_ids))
            )
        ).all()
        event_rows = (
            await self.db.execute(
                select(Event.event_id, Event.title).where(Event.created_by.in_(teammate_ids))
            )
        ).all()
        election_titles = {r.election_id: r.title for r in election_rows}
        event_titles = {r.event_id: r.title for r in event_rows}
        election_ids = list(election_titles.keys())
        event_ids = list(event_titles.keys())
        if not election_ids and not event_ids:
            return []

        # Category.candidates is lazy="selectin" — eagerly loaded here for free.
        categories = (
            await self.db.execute(
                select(Category).where(
                    or_(Category.election_id.in_(election_ids), Category.event_id.in_(event_ids))
                )
            )
        ).scalars().all()
        if not categories:
            return []

        votes = (
            await self.db.execute(
                select(Vote).where(or_(Vote.election_id.in_(election_ids), Vote.event_id.in_(event_ids)))
            )
        ).scalars().all()

        category_results = tally_votes_by_category(list(votes), list(categories), CryptographyService())
        category_by_id = {c.category_id: c for c in categories}

        nominees: List[NomineeResponse] = []
        for cat_result in category_results:
            category = category_by_id[cat_result.category_id]
            candidates_by_id = {c.candidate_id: c for c in category.candidates}
            is_election = category.election_id is not None
            parent_id = category.election_id if is_election else category.event_id
            parent_title = election_titles[parent_id] if is_election else event_titles[parent_id]

            for rank, cr in enumerate(cat_result.results):
                candidate = candidates_by_id.get(cr.candidate_id)
                if not candidate:
                    continue
                nominees.append(
                    NomineeResponse(
                        candidate_id=candidate.candidate_id,
                        name=candidate.name,
                        short_code=candidate.short_code,
                        image_url=candidate.image_url,
                        category_id=category.category_id,
                        category_name=category.name,
                        parent_id=parent_id,
                        parent_type="election" if is_election else "event",
                        parent_title=parent_title,
                        vote_count=cr.vote_count,
                        category_rank=rank + 1,
                        created_at=candidate.created_at,
                    )
                )
        return nominees
