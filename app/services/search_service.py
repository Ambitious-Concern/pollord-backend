from typing import List
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.election import Candidate, Election
from app.models.event import Event, TicketType
from app.schemas.search import SearchResultItem

LIMIT_PER_GROUP = 4


class SearchService:
    """One search across everything the organizer dashboard's command
    palette needs — elections, events, nominees, ticket types — scoped to
    the caller's team exactly like each domain's own list endpoint."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def search(self, teammate_ids: List[UUID], q: str) -> List[SearchResultItem]:
        q = q.strip()
        if not q or not teammate_ids:
            return []
        like = f"%{q}%"
        items: List[SearchResultItem] = []

        election_rows = (
            await self.db.execute(
                select(Election.election_id, Election.title, Election.venue, Election.status)
                .where(Election.created_by.in_(teammate_ids), Election.title.ilike(like))
                .order_by(Election.created_at.desc())
                .limit(LIMIT_PER_GROUP)
            )
        ).all()
        items += [
            SearchResultItem(
                id=r.election_id, type="election", title=r.title,
                subtitle=r.venue or "Not yet published", status=r.status,
            )
            for r in election_rows
        ]

        event_rows = (
            await self.db.execute(
                select(Event.event_id, Event.title, Event.location, Event.status)
                .where(Event.created_by.in_(teammate_ids), Event.title.ilike(like))
                .order_by(Event.created_at.desc())
                .limit(LIMIT_PER_GROUP)
            )
        ).all()
        items += [
            SearchResultItem(
                id=r.event_id, type="event", title=r.title,
                subtitle=r.location or "Not yet published", status=r.status,
            )
            for r in event_rows
        ]

        # Candidate.election_id/event_id are denormalized, not FK-enforced
        # (kept in sync by the service layer) — fine here since search is
        # read-only and already scoped by the owned-id subqueries below.
        owned_election_ids = select(Election.election_id).where(Election.created_by.in_(teammate_ids))
        owned_event_ids = select(Event.event_id).where(Event.created_by.in_(teammate_ids))

        candidate_rows = (
            await self.db.execute(
                select(
                    Candidate.candidate_id, Candidate.name, Candidate.short_code,
                    Election.title.label("election_title"), Event.title.label("event_title"),
                )
                .outerjoin(Election, Candidate.election_id == Election.election_id)
                .outerjoin(Event, Candidate.event_id == Event.event_id)
                .where(
                    Candidate.name.ilike(like),
                    or_(
                        Candidate.election_id.in_(owned_election_ids),
                        Candidate.event_id.in_(owned_event_ids),
                    ),
                )
                .order_by(Candidate.created_at.desc())
                .limit(LIMIT_PER_GROUP)
            )
        ).all()
        items += [
            SearchResultItem(
                id=r.candidate_id, type="nominee", title=r.name,
                subtitle=" · ".join(p for p in (r.short_code, r.election_title or r.event_title) if p),
            )
            for r in candidate_rows
        ]

        ticket_rows = (
            await self.db.execute(
                select(TicketType.ticket_type_id, TicketType.type_name, Event.title.label("event_title"))
                .join(Event, TicketType.event_id == Event.event_id)
                .where(TicketType.event_id.in_(owned_event_ids), TicketType.type_name.ilike(like))
                .order_by(TicketType.created_at.desc())
                .limit(LIMIT_PER_GROUP)
            )
        ).all()
        items += [
            SearchResultItem(id=r.ticket_type_id, type="ticket_type", title=r.type_name, subtitle=r.event_title)
            for r in ticket_rows
        ]

        return items
