from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.payout_request import PayoutRequest
from app.repositories.base import BaseRepository


class PayoutRequestRepository(BaseRepository[PayoutRequest]):
    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_by_event(self, event_id: UUID) -> List[PayoutRequest]:
        result = await self.session.execute(
            select(PayoutRequest)
            .where(PayoutRequest.event_id == event_id)
            .order_by(PayoutRequest.requested_at.desc())
        )
        return list(result.scalars().all())

    async def get_by_organizer(self, organizer_id: UUID) -> List[PayoutRequest]:
        result = await self.session.execute(
            select(PayoutRequest)
            .where(PayoutRequest.organizer_id == organizer_id)
            .options(selectinload(PayoutRequest.event))
            .order_by(PayoutRequest.requested_at.desc())
        )
        return list(result.scalars().all())

    async def get_all(self, status: Optional[str] = None) -> List[PayoutRequest]:
        query = select(PayoutRequest).options(
            selectinload(PayoutRequest.event), selectinload(PayoutRequest.organizer)
        )
        if status:
            query = query.where(PayoutRequest.status == status)
        result = await self.session.execute(query.order_by(PayoutRequest.requested_at.desc()))
        return list(result.scalars().all())

    async def get_total_requested_for_event(
        self, event_id: UUID, exclude_status: str = "rejected"
    ) -> float:
        """Sum of amounts already requested (pending or paid) for an event —
        subtracted from gross revenue so an organizer can't request the same
        money twice."""
        result = await self.session.execute(
            select(PayoutRequest).where(
                PayoutRequest.event_id == event_id,
                PayoutRequest.status != exclude_status,
            )
        )
        return float(sum(r.amount for r in result.scalars().all()))

    async def has_pending(self, event_id: UUID) -> bool:
        result = await self.session.execute(
            select(PayoutRequest.payout_request_id).where(
                PayoutRequest.event_id == event_id,
                PayoutRequest.status == "pending",
            )
        )
        return result.first() is not None

    async def mark_reviewed(
        self,
        payout_request_id: UUID,
        status: str,
        admin_notes: Optional[str],
        reviewed_by: UUID,
    ) -> Optional[PayoutRequest]:
        req = await self.get_by_id(payout_request_id, id_field="payout_request_id")
        if not req:
            return None
        req.status = status
        req.admin_notes = admin_notes
        req.reviewed_at = datetime.now(timezone.utc)
        req.reviewed_by = reviewed_by
        await self.session.flush()
        await self.session.refresh(req)
        return req
