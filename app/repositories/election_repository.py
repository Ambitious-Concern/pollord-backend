from datetime import datetime, timezone
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.election import Candidate, Category, Election, EligibleVoter
from app.models.user import User
from app.repositories.base import BaseRepository


class ElectionRepository(BaseRepository[Election]):
    """Election queries: eligibility, USSD/slug lookup, creator-scoped listing."""

    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_by_org(self, org_id: UUID) -> List[Election]:
        result = await self.session.execute(select(Election).where(Election.org_id == org_id))
        return list(result.scalars().all())

    async def get_with_categories(self, election_id: UUID) -> Optional[Election]:
        result = await self.session.execute(
            select(Election)
            .options(selectinload(Election.categories).selectinload(Category.candidates))
            .where(Election.election_id == election_id)
        )
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Optional[Election]:
        result = await self.session.execute(
            select(Election)
            .options(selectinload(Election.categories).selectinload(Category.candidates))
            .where(Election.slug == slug)
        )
        return result.scalar_one_or_none()

    async def get_by_ussd_code(self, code: str) -> Optional[Election]:
        result = await self.session.execute(
            select(Election)
            .options(selectinload(Election.categories).selectinload(Category.candidates))
            .where(Election.ussd_code == code)
        )
        return result.scalar_one_or_none()

    async def get_active_elections(self) -> List[Election]:
        now = datetime.now(timezone.utc)
        result = await self.session.execute(
            select(Election)
            .options(selectinload(Election.categories).selectinload(Category.candidates))
            .where(
                Election.status == "active",
                Election.start_datetime <= now,
                Election.end_datetime >= now,
            )
        )
        return list(result.scalars().all())

    async def get_elections_by_creator(
        self,
        user_id: UUID,
        skip: int = 0,
        limit: int = 20,
        status: Optional[str] = None,
    ) -> List[Election]:
        query = select(Election).where(Election.created_by == user_id)
        if status:
            query = query.where(Election.status == status)
        query = query.order_by(Election.created_at.desc()).offset(skip).limit(limit)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_elections_by_creators(
        self,
        user_ids: List[UUID],
        skip: int = 0,
        limit: int = 20,
        status: Optional[str] = None,
    ) -> List[Election]:
        """Elections created by any of the given users — i.e. by anyone on the
        caller's team."""
        if not user_ids:
            return []
        query = select(Election).where(Election.created_by.in_(user_ids))
        if status:
            query = query.where(Election.status == status)
        query = query.order_by(Election.created_at.desc()).offset(skip).limit(limit)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_public_elections(
        self,
        skip: int = 0,
        limit: int = 20,
        status: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[Election]:
        """The public listings page's source query — visibility filtering
        and the title search both happen here in SQL rather than after the
        fact in the endpoint, so a search term narrows the actual page of
        rows fetched instead of being applied to whatever page happened to
        come back."""
        query = select(Election).where(Election.visibility == "public")
        if status:
            query = query.where(Election.status == status)
        if search:
            query = query.where(Election.title.ilike(f"%{search}%"))
        query = query.order_by(Election.created_at.desc()).offset(skip).limit(limit)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_elections_by_status(
        self, status: str, skip: int = 0, limit: int = 20
    ) -> List[Election]:
        result = await self.session.execute(
            select(Election)
            .where(Election.status == status)
            .offset(skip)
            .limit(limit)
            .order_by(Election.created_at.desc())
        )
        return list(result.scalars().all())

    async def update_status(self, election_id: UUID, status: str) -> Optional[Election]:
        election = await self.get_by_id(election_id, id_field="election_id")
        if election:
            election.status = status
            await self.session.flush()
            await self.session.refresh(election)
        return election

    async def is_user_eligible(self, election_id: UUID, user_id: UUID) -> bool:
        result = await self.session.execute(
            select(EligibleVoter).where(
                EligibleVoter.election_id == election_id,
                EligibleVoter.user_id == user_id,
            )
        )
        return result.scalar_one_or_none() is not None

    async def add_eligible_voters(
        self, election_id: UUID, user_ids: List[UUID]
    ) -> List[EligibleVoter]:
        voters = []
        for uid in user_ids:
            existing = await self.session.execute(
                select(EligibleVoter).where(
                    EligibleVoter.election_id == election_id,
                    EligibleVoter.user_id == uid,
                )
            )
            if existing.scalar_one_or_none() is None:
                voter = EligibleVoter(election_id=election_id, user_id=uid)
                self.session.add(voter)
                voters.append(voter)
        await self.session.flush()
        return voters

    async def get_eligible_voters(self, election_id: UUID) -> List[EligibleVoter]:
        result = await self.session.execute(
            select(EligibleVoter)
            .options(selectinload(EligibleVoter.user))
            .where(EligibleVoter.election_id == election_id)
        )
        return list(result.scalars().all())

    async def count_eligible_voters(self, election_id: UUID) -> int:
        from sqlalchemy import func
        result = await self.session.execute(
            select(func.count()).select_from(EligibleVoter).where(
                EligibleVoter.election_id == election_id
            )
        )
        return result.scalar_one()

    async def get_eligible_voters_for_elections(
        self, election_ids: List[UUID], search: Optional[str] = None
    ) -> List[EligibleVoter]:
        """Cross-election view for the Voters dashboard page — same rows
        get_eligible_voters returns per-election, just spanning every gated
        election passed in at once. Unpaginated: used for the "not yet
        voted" sort preset, whose key (has_voted) is computed from
        anonymized vote hashes in Python and so can't drive a SQL ORDER BY
        + LIMIT/OFFSET — the caller sorts and slices this full set itself."""
        if not election_ids:
            return []
        query = (
            select(EligibleVoter)
            .join(User, EligibleVoter.user_id == User.user_id)
            .options(selectinload(EligibleVoter.user))
            .where(EligibleVoter.election_id.in_(election_ids))
        )
        if search:
            like = f"%{search}%"
            query = query.where(or_(User.full_name.ilike(like), User.email.ilike(like)))
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_eligible_voters_for_elections_page(
        self,
        election_ids: List[UUID],
        search: Optional[str] = None,
        sort: str = "newest",
        skip: int = 0,
        limit: int = 20,
    ) -> Tuple[List[EligibleVoter], int]:
        """SQL-side paginated variant for the two sort presets that map to a
        real column (created_at, name). See get_eligible_voters_for_elections
        for the has_voted-sort fallback."""
        if not election_ids:
            return [], 0

        filters = [EligibleVoter.election_id.in_(election_ids)]
        if search:
            like = f"%{search}%"
            filters.append(or_(User.full_name.ilike(like), User.email.ilike(like)))

        total = (
            await self.session.execute(
                select(func.count(EligibleVoter.eligible_voter_id))
                .join(User, EligibleVoter.user_id == User.user_id)
                .where(*filters)
            )
        ).scalar_one()

        query = (
            select(EligibleVoter)
            .join(User, EligibleVoter.user_id == User.user_id)
            .options(selectinload(EligibleVoter.user))
            .where(*filters)
        )
        query = query.order_by(User.full_name.asc() if sort == "name" else EligibleVoter.created_at.desc())
        result = await self.session.execute(query.offset(skip).limit(limit))
        return list(result.scalars().all()), total

    async def get_eligible_voter_by_id(self, eligible_voter_id: UUID) -> Optional[EligibleVoter]:
        result = await self.session.execute(
            select(EligibleVoter)
            .options(selectinload(EligibleVoter.user), selectinload(EligibleVoter.election))
            .where(EligibleVoter.eligible_voter_id == eligible_voter_id)
        )
        return result.scalar_one_or_none()

    async def remove_eligible_voter(self, eligible_voter_id: UUID) -> bool:
        voter = await self.session.get(EligibleVoter, eligible_voter_id)
        if voter is None:
            return False
        await self.session.delete(voter)
        await self.session.flush()
        return True


class CategoryRepository(BaseRepository[Category]):
    """Category queries, scoped by election or event parent."""

    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_by_election(self, election_id: UUID) -> List[Category]:
        result = await self.session.execute(
            select(Category)
            .options(selectinload(Category.candidates))
            .where(Category.election_id == election_id)
            .order_by(Category.display_order)
        )
        return list(result.scalars().all())

    async def get_by_event(self, event_id: UUID) -> List[Category]:
        result = await self.session.execute(
            select(Category)
            .options(selectinload(Category.candidates))
            .where(Category.event_id == event_id)
            .order_by(Category.display_order)
        )
        return list(result.scalars().all())

    async def get_by_elections(self, election_ids: List[UUID]) -> List[Category]:
        if not election_ids:
            return []
        result = await self.session.execute(
            select(Category)
            .where(Category.election_id.in_(election_ids))
            .order_by(Category.display_order)
        )
        return list(result.scalars().all())

    async def get_by_events(self, event_ids: List[UUID]) -> List[Category]:
        if not event_ids:
            return []
        result = await self.session.execute(
            select(Category)
            .where(Category.event_id.in_(event_ids))
            .order_by(Category.display_order)
        )
        return list(result.scalars().all())

    async def get_with_candidates(self, category_id: UUID) -> Optional[Category]:
        result = await self.session.execute(
            select(Category)
            .options(selectinload(Category.candidates))
            .where(Category.category_id == category_id)
        )
        return result.scalar_one_or_none()


class CandidateRepository(BaseRepository[Candidate]):
    """Candidate queries, scoped by election, event, or category."""

    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_by_election(self, election_id: UUID) -> List[Candidate]:
        result = await self.session.execute(
            select(Candidate)
            .where(Candidate.election_id == election_id)
            .order_by(Candidate.display_order)
        )
        return list(result.scalars().all())

    async def get_by_event(self, event_id: UUID) -> List[Candidate]:
        result = await self.session.execute(
            select(Candidate)
            .where(Candidate.event_id == event_id)
            .order_by(Candidate.display_order)
        )
        return list(result.scalars().all())

    async def get_by_category(self, category_id: UUID) -> List[Candidate]:
        result = await self.session.execute(
            select(Candidate)
            .where(Candidate.category_id == category_id)
            .order_by(Candidate.display_order)
        )
        return list(result.scalars().all())
