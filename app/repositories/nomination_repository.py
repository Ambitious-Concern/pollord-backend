from typing import List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.nomination import NominationForm, NominationFormField, NominationSubmission
from app.repositories.base import BaseRepository


class NominationFormRepository(BaseRepository[NominationForm]):
    """Form CRUD, including the wholesale field replace the builder's
    save-the-whole-form UX needs."""

    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_with_fields(self, form_id: UUID) -> Optional[NominationForm]:
        result = await self.session.execute(
            select(NominationForm)
            .options(selectinload(NominationForm.fields))
            .where(NominationForm.form_id == form_id)
        )
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Optional[NominationForm]:
        result = await self.session.execute(
            select(NominationForm)
            .options(selectinload(NominationForm.fields))
            .where(NominationForm.slug == slug)
        )
        return result.scalar_one_or_none()

    async def get_by_election(self, election_id: UUID) -> List[NominationForm]:
        result = await self.session.execute(
            select(NominationForm)
            .options(selectinload(NominationForm.fields))
            .where(NominationForm.election_id == election_id)
            .order_by(NominationForm.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_by_elections(self, election_ids: List[UUID]) -> List[NominationForm]:
        if not election_ids:
            return []
        result = await self.session.execute(
            select(NominationForm)
            .options(selectinload(NominationForm.fields))
            .where(NominationForm.election_id.in_(election_ids))
            .order_by(NominationForm.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_by_event(self, event_id: UUID) -> List[NominationForm]:
        result = await self.session.execute(
            select(NominationForm)
            .options(selectinload(NominationForm.fields))
            .where(NominationForm.event_id == event_id)
            .order_by(NominationForm.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_by_events(self, event_ids: List[UUID]) -> List[NominationForm]:
        if not event_ids:
            return []
        result = await self.session.execute(
            select(NominationForm)
            .options(selectinload(NominationForm.fields))
            .where(NominationForm.event_id.in_(event_ids))
            .order_by(NominationForm.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_first_public_by_election(self, election_id: UUID) -> Optional[NominationForm]:
        """Returns the first public form for a listing detail page's Nominate button."""
        result = await self.session.execute(
            select(NominationForm)
            .where(
                NominationForm.election_id == election_id,
                NominationForm.visibility == "public",
            )
            .order_by(NominationForm.created_at.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_first_public_by_event(self, event_id: UUID) -> Optional[NominationForm]:
        """Returns the first public form for a listing detail page's Nominate button."""
        result = await self.session.execute(
            select(NominationForm)
            .where(
                NominationForm.event_id == event_id,
                NominationForm.visibility == "public",
            )
            .order_by(NominationForm.created_at.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def replace_fields(self, form_id: UUID, fields: List[dict]) -> None:
        """Deletes every existing custom field and re-inserts the given
        list in order — matches the builder always submitting its full,
        already-ordered field list rather than incremental moves."""
        existing = await self.session.execute(
            select(NominationFormField).where(NominationFormField.form_id == form_id)
        )
        for f in existing.scalars().all():
            await self.session.delete(f)
        await self.session.flush()

        for order, data in enumerate(fields):
            self.session.add(NominationFormField(form_id=form_id, display_order=order, **data))
        await self.session.flush()

        # If the parent NominationForm is already in the identity map (e.g.
        # the caller loaded it with .fields eager-loaded before calling this),
        # a later selectinload re-query would otherwise reuse that stale,
        # already-populated collection instead of seeing this replacement.
        form = await self.session.get(NominationForm, form_id)
        if form:
            self.session.expire(form, ["fields"])


class NominationSubmissionRepository(BaseRepository[NominationSubmission]):
    """Submission writes and the review-queue's grouped read."""

    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_by_election(self, election_id: UUID) -> List[NominationSubmission]:
        result = await self.session.execute(
            select(NominationSubmission)
            .where(NominationSubmission.election_id == election_id)
            .order_by(NominationSubmission.submitted_at.asc())
        )
        return list(result.scalars().all())

    async def get_by_elections(self, election_ids: List[UUID]) -> List[NominationSubmission]:
        if not election_ids:
            return []
        result = await self.session.execute(
            select(NominationSubmission)
            .where(NominationSubmission.election_id.in_(election_ids))
            .order_by(NominationSubmission.submitted_at.asc())
        )
        return list(result.scalars().all())

    async def get_by_event(self, event_id: UUID) -> List[NominationSubmission]:
        result = await self.session.execute(
            select(NominationSubmission)
            .where(NominationSubmission.event_id == event_id)
            .order_by(NominationSubmission.submitted_at.asc())
        )
        return list(result.scalars().all())

    async def get_by_events(self, event_ids: List[UUID]) -> List[NominationSubmission]:
        if not event_ids:
            return []
        result = await self.session.execute(
            select(NominationSubmission)
            .where(NominationSubmission.event_id.in_(event_ids))
            .order_by(NominationSubmission.submitted_at.asc())
        )
        return list(result.scalars().all())

    async def get_group_by_event(
        self, event_id: UUID, category_id: UUID, nominee_name: str
    ) -> List[NominationSubmission]:
        result = await self.session.execute(
            select(NominationSubmission).where(
                NominationSubmission.event_id == event_id,
                NominationSubmission.category_id == category_id,
                NominationSubmission.nominee_name == nominee_name,
            )
        )
        return list(result.scalars().all())

    async def set_group_status_by_event(
        self, event_id: UUID, category_id: UUID, nominee_name: str, status: str
    ) -> int:
        submissions = await self.get_group_by_event(event_id, category_id, nominee_name)
        for s in submissions:
            s.status = status
        await self.session.flush()
        return len(submissions)

    async def get_group(
        self, election_id: UUID, category_id: UUID, nominee_name: str
    ) -> List[NominationSubmission]:
        result = await self.session.execute(
            select(NominationSubmission).where(
                NominationSubmission.election_id == election_id,
                NominationSubmission.category_id == category_id,
                NominationSubmission.nominee_name == nominee_name,
            )
        )
        return list(result.scalars().all())

    async def set_group_status(
        self, election_id: UUID, category_id: UUID, nominee_name: str, status: str
    ) -> int:
        submissions = await self.get_group(election_id, category_id, nominee_name)
        for s in submissions:
            s.status = status
        await self.session.flush()
        return len(submissions)
