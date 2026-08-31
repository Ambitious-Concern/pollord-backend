import secrets
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
from app.schemas.payout import (
    MobileMoneyNetwork,
    PayoutAvailableResponse,
    PayoutRequestCreate,
    PayoutRequestResponse,
)
from app.services.paystack_service import PaystackService


class PayoutService:
    def __init__(
        self,
        payout_repo: PayoutRequestRepository,
        event_repo: EventRepository,
        purchase_repo: TicketPurchaseRepository,
        user_repo: UserRepository,
        paystack: Optional[PaystackService] = None,
    ):
        self.payout_repo = payout_repo
        self.event_repo = event_repo
        self.purchase_repo = purchase_repo
        self.user_repo = user_repo
        self.paystack = paystack

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
            payout_method=req.payout_method,
            recipient_name=req.recipient_name,
            mobile_network=req.mobile_network,
            mobile_number=req.mobile_number,
            transfer_reference=req.transfer_reference,
            transfer_status=req.transfer_status,
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

    async def request_payout(
        self, event_id: UUID, user: User, data: PayoutRequestCreate
    ) -> PayoutRequestResponse:
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
                "payout_method": data.payout_method,
                "recipient_name": data.recipient_name,
                "mobile_network": data.mobile_network,
                "mobile_number": data.mobile_number,
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

    async def list_mobile_money_networks(self) -> List[MobileMoneyNetwork]:
        """Fetched live from Paystack rather than hardcoded — a wrong network
        code here means a transfer could go to the wrong destination."""
        assert self.paystack is not None
        banks = await self.paystack.list_banks(currency="GHS", transfer_type="mobile_money")
        return [MobileMoneyNetwork(name=b["name"], code=b["code"]) for b in banks]

    async def initiate_transfer(
        self, payout_request_id: UUID, admin: User
    ) -> PayoutRequestResponse:
        """Pays a pending request out via Paystack Transfer, called from the
        admin console. Only flips status to "paid" if Paystack confirms the
        transfer actually succeeded — see record_transfer_result."""
        assert self.paystack is not None
        req = await self.payout_repo.get_by_id(payout_request_id, id_field="payout_request_id")
        if not req:
            raise HTTPException(status_code=404, detail="Payout request not found")
        if req.status != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"This request is already {req.status}, nothing to pay",
            )
        if not (req.mobile_network and req.mobile_number and req.recipient_name):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "This request has no payout destination on file (it may predate "
                    "this feature). Pay the organizer manually and mark it paid instead."
                ),
            )

        # Confirm the number actually resolves to a real account before we
        # ever create a recipient or move money against it.
        resolved = await self.paystack.resolve_account(req.mobile_number, req.mobile_network)

        recipient_code = req.paystack_recipient_code
        if not recipient_code:
            recipient = await self.paystack.create_transfer_recipient(
                name=resolved.get("account_name") or req.recipient_name,
                account_number=req.mobile_number,
                bank_code=req.mobile_network,
                currency="GHS",
                recipient_type="mobile_money",
            )
            recipient_code = recipient["recipient_code"]
            await self.payout_repo.set_recipient_code(payout_request_id, recipient_code)

        event = await self.event_repo.get_by_id(req.event_id, id_field="event_id")
        reference = f"payout_{secrets.token_urlsafe(16)}"
        transfer = await self.paystack.initiate_transfer(
            amount=int(req.amount * 100),  # pesewas
            recipient_code=recipient_code,
            reason=f"Pollord payout: {event.title if event else req.event_id}",
            reference=reference,
        )

        # From here on, Paystack has already accepted (or queued, or
        # rejected) the transfer — a real-world action has happened. This
        # DB write recording that outcome must land no matter what, so we
        # deliberately never raise past this point: get_db() rolls back the
        # session on any exception, and rolling back here would make the
        # request look untouched — retrying would call Paystack a second
        # time for money that may already be moving. Callers read
        # `transfer_status` in the response body instead of an HTTP error.
        transfer_status = transfer.get("status", "unknown")
        updated = await self.payout_repo.record_transfer_result(
            payout_request_id,
            transfer_reference=reference,
            transfer_status=transfer_status,
            mark_paid=(transfer_status == "success"),
            reviewed_by=admin.user_id,
        )
        if not updated:
            # Should be unreachable (we already loaded `req` above), but
            # fall back to the pre-update object rather than raise.
            updated = req

        organizer = await self.user_repo.get_by_id(req.organizer_id, id_field="user_id")
        return self._to_response(updated, event, organizer)
