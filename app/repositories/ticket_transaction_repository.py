from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ticket_transaction import TicketTransaction
from app.repositories.base import BaseRepository


class TicketTransactionRepository(BaseRepository[TicketTransaction]):
    def __init__(self, session: AsyncSession):
        super().__init__(TicketTransaction, session)

    async def get_by_reference(self, reference: str) -> Optional[TicketTransaction]:
        result = await self.session.execute(
            select(TicketTransaction).where(TicketTransaction.reference == reference)
        )
        return result.scalar_one_or_none()

    async def update_status(
        self, reference: str, status: str, paystack_response: dict | None = None
    ) -> Optional[TicketTransaction]:
        txn = await self.get_by_reference(reference)
        if not txn:
            return None
        txn.status = status
        if paystack_response is not None:
            txn.paystack_response = paystack_response
        await self.session.flush()
        await self.session.refresh(txn)
        return txn
