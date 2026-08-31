from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.election import Candidate, Category, Election, EligibleVoter
from app.repositories.base import BaseRepository


class ElectionRepository(BaseRepository[Election]):
    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

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
            select(EligibleVoter).where(EligibleVoter.election_id == election_id)
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


class CategoryRepository(BaseRepository[Category]):
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

    async def get_with_candidates(self, category_id: UUID) -> Optional[Category]:
        result = await self.session.execute(
            select(Category)
            .options(selectinload(Category.candidates))
            .where(Category.category_id == category_id)
        )
        return result.scalar_one_or_none()


class CandidateRepository(BaseRepository[Candidate]):
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
