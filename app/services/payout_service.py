from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException, status

from app.models.event import Event
from app.models.payout_request import PayoutRequest
from app.models.user import User
from app.repositories.event_repository import EventRepository
from app.repositories.payout_request_repository import PayoutRequestRepository
from app.repositories.ticket_repository import TicketPurchaseRepository
from app.repositories.user_repository import UserRepository
from app.schemas.payout import PayoutAvailableResponse, PayoutRequestResponse


class PayoutService:
    def __init__(
        self,
        payout_repo: PayoutRequestRepository,
        event_repo: EventRepository,
        purchase_repo: TicketPurchaseRepository,
        user_repo: UserRepository,
    ):
        self.payout_repo = payout_repo
        self.event_repo = event_repo
        self.purchase_repo = purchase_repo
        self.user_repo = user_repo

    @staticmethod
    def _to_response(
        req: PayoutRequest, event: Optional[Event], organizer: Optional[User]
    ) -> PayoutRequestResponse:
        return PayoutRequestResponse(
            payout_request_id=req.payout_request_id,
            event_id=req.event_id,
            event_title=event.title if event else "",
            organizer_id=req.organizer_id,
            organizer_name=organizer.full_name if organizer else "",
            organizer_email=organizer.email if organizer else "",
            amount=req.amount,
            status=req.status,
            admin_notes=req.admin_notes,
            requested_at=req.requested_at,
            reviewed_at=req.reviewed_at,
        )

    async def _require_event_and_ownership(self, event_id: UUID, user: User) -> Event:
        event = await self.event_repo.get_by_id(event_id, id_field="event_id")
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        is_admin = any(ur.role.role_name == "System Administrator" for ur in user.user_roles)
        if event.created_by != user.user_id and not is_admin:
            raise HTTPException(status_code=403, detail="You do not have access to this event")
        return event

    async def get_available(self, event_id: UUID, user: User) -> PayoutAvailableResponse:
        await self._require_event_and_ownership(event_id, user)
        gross = Decimal(str(await self.purchase_repo.get_revenue_by_event(event_id)))
        already_requested = Decimal(
            str(await self.payout_repo.get_total_requested_for_event(event_id))
        )
        return PayoutAvailableResponse(
            event_id=event_id,
            gross_revenue=gross,
            already_requested=already_requested,
            available=max(gross - already_requested, Decimal("0")),
            has_pending_request=await self.payout_repo.has_pending(event_id),
        )

    async def request_payout(self, event_id: UUID, user: User) -> PayoutRequestResponse:
        event = await self._require_event_and_ownership(event_id, user)

        if await self.payout_repo.has_pending(event_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="There is already a pending payout request for this event",
            )

        gross = Decimal(str(await self.purchase_repo.get_revenue_by_event(event_id)))
        already_requested = Decimal(
            str(await self.payout_repo.get_total_requested_for_event(event_id))
        )
        available = gross - already_requested
        if available <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No revenue available to request a payout for",
            )

        req = await self.payout_repo.create(
            {
                "event_id": event_id,
                "organizer_id": user.user_id,
                "amount": available,
                "status": "pending",
            }
        )
        return self._to_response(req, event, user)

    async def list_for_event(self, event_id: UUID, user: User) -> List[PayoutRequestResponse]:
        event = await self._require_event_and_ownership(event_id, user)
        requests = await self.payout_repo.get_by_event(event_id)
        return [self._to_response(r, event, user) for r in requests]

    async def list_mine(self, organizer_id: UUID) -> List[PayoutRequestResponse]:
        requests = await self.payout_repo.get_by_organizer(organizer_id)
        # get_by_organizer eager-loads `event`; organizer is always the caller.
        organizer = await self.user_repo.get_by_id(organizer_id, id_field="user_id")
        return [self._to_response(r, r.event, organizer) for r in requests]

    async def list_all(self, status_filter: Optional[str] = None) -> List[PayoutRequestResponse]:
        requests = await self.payout_repo.get_all(status_filter)
        # get_all eager-loads both `event` and `organizer`.
        return [self._to_response(r, r.event, r.organizer) for r in requests]

    async def review(
        self, payout_request_id: UUID, new_status: str, admin_notes: Optional[str], admin: User
    ) -> PayoutRequestResponse:
        if new_status not in ("paid", "rejected"):
            raise HTTPException(status_code=400, detail="status must be 'paid' or 'rejected'")
        req = await self.payout_repo.mark_reviewed(
            payout_request_id, new_status, admin_notes, admin.user_id
        )
        if not req:
            raise HTTPException(status_code=404, detail="Payout request not found")
        event = await self.event_repo.get_by_id(req.event_id, id_field="event_id")
        organizer = await self.user_repo.get_by_id(req.organizer_id, id_field="user_id")
        return self._to_response(req, event, organizer)
