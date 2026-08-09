import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException, status

from app.core.security import generate_secure_token
from app.models.event import Event
from app.models.ticket import Ticket
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.event_repository import EventRepository, TicketTypeRepository
from app.repositories.ticket_repository import TicketPurchaseRepository, TicketRepository
from app.repositories.ticket_transaction_repository import TicketTransactionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.ticket import (
    TicketPurchaseRequest,
    TicketPurchaseResponse,
    TicketResponse,
    TicketValidationResponse,
)
from app.services.email_service import send_email, ticket_confirmation_email

logger = logging.getLogger(__name__)


class TicketingService:
    def __init__(
        self,
        event_repo: EventRepository,
        ticket_type_repo: TicketTypeRepository,
        ticket_repo: TicketRepository,
        purchase_repo: TicketPurchaseRepository,
        audit_repo: AuditLogRepository,
        user_repo: UserRepository,
    ):
        self.event_repo = event_repo
        self.ticket_type_repo = ticket_type_repo
        self.ticket_repo = ticket_repo
        self.purchase_repo = purchase_repo
        self.audit_repo = audit_repo
        self.user_repo = user_repo

    async def purchase_tickets(
        self,
        user_id: UUID,
        data: TicketPurchaseRequest,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> TicketPurchaseResponse:
        # 1. Verify event
        event = await self.event_repo.get_with_ticket_types(data.event_id)
        if not event:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Event not found",
            )
        if event.status != "published":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Event is not available for ticket purchase",
            )

        now = datetime.now(timezone.utc)
        total_amount = Decimal("0.00")
        ticket_items = []

        # 2. Validate each item
        type_map = {tt.ticket_type_id: tt for tt in event.ticket_types}

        for item in data.items:
            ticket_type = type_map.get(item.ticket_type_id)
            if not ticket_type:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Ticket type {item.ticket_type_id} not found for this event",
                )

            if ticket_type.status != "active":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Ticket type '{ticket_type.type_name}' is not available",
                )

            # Check sales window
            if ticket_type.sales_start_datetime and now < ticket_type.sales_start_datetime:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Sales for '{ticket_type.type_name}' have not started yet",
                )
            if ticket_type.sales_end_datetime and now > ticket_type.sales_end_datetime:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Sales for '{ticket_type.type_name}' have ended",
                )

            # Check per-user limit
            existing_count = await self.ticket_repo.count_user_tickets_for_type(
                user_id, item.ticket_type_id
            )
            if existing_count + item.quantity > ticket_type.max_per_user:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Maximum {ticket_type.max_per_user} tickets per user for '{ticket_type.type_name}'",
                )

            # Atomically decrement stock
            success = await self.ticket_type_repo.decrement_available(
                item.ticket_type_id, item.quantity
            )
            if not success:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Not enough tickets available for '{ticket_type.type_name}'",
                )

            total_amount += ticket_type.price * item.quantity
            ticket_items.append((ticket_type, item.quantity))

        # 3. Create purchase record
        payment_status = "completed" if total_amount == 0 else "completed"
        purchase = await self.purchase_repo.create(
            {
                "user_id": user_id,
                "event_id": data.event_id,
                "total_amount": total_amount,
                "payment_status": payment_status,
            }
        )

        # 4. Generate tickets
        tickets = []
        for ticket_type, quantity in ticket_items:
            for _ in range(quantity):
                ticket_code = generate_secure_token(16)
                qr_data = json.dumps(
                    {
                        "ticket_code": ticket_code,
                        "event_id": str(data.event_id),
                        "ticket_type": ticket_type.type_name,
                    }
                )
                ticket = await self.ticket_repo.create(
                    {
                        "ticket_code": ticket_code,
                        "event_id": data.event_id,
                        "ticket_type_id": ticket_type.ticket_type_id,
                        "user_id": user_id,
                        "purchase_id": purchase.purchase_id,
                        "qr_code_data": qr_data,
                    }
                )
                tickets.append(ticket)

        # 5. Audit log
        await self.audit_repo.log_action(
            action_type="TICKET_PURCHASE",
            entity_type="Event",
            entity_id=data.event_id,
            user_id=user_id,
            changes={"total_amount": str(total_amount), "ticket_count": len(tickets)},
            ip_address=ip,
            user_agent=user_agent,
        )

        # Send confirmation email (best-effort — never blocks the purchase)
        buyer = await self.user_repo.get_by_id(user_id, id_field="user_id")
        if buyer and buyer.email:
            subject, html = ticket_confirmation_email(event.title, len(tickets))
            send_email(buyer.email, subject, html)

        return TicketPurchaseResponse(
            purchase_id=purchase.purchase_id,
            event_id=data.event_id,
            total_amount=total_amount,
            payment_status=purchase.payment_status,
            tickets=[
                TicketResponse(
                    ticket_id=t.ticket_id,
                    ticket_code=t.ticket_code,
                    event_id=t.event_id,
                    ticket_type_id=t.ticket_type_id,
                    ticket_status=t.ticket_status,
                    purchase_date=t.purchase_date,
                )
                for t in tickets
            ],
            purchased_at=purchase.purchased_at,
        )

    async def _mark_needs_refund(
        self,
        txn_repo: "TicketTransactionRepository",
        reference: str,
        paystack_data: dict,
        *,
        transaction_id: UUID,
        user_id: UUID,
        reason: str,
        ticket_type_id: UUID,
        detail: str,
        decremented_so_far: Optional[List["tuple[UUID, int]"]] = None,
    ) -> None:
        """Persist a needs_refund outcome and raise the 409 that reports it.

        `decremented_so_far` lists the (ticket_type_id, quantity) pairs for
        earlier items in this same multi-item purchase whose stock decrement
        already succeeded before this later item failed. We compensate those
        with `increment_available` (rather than rolling back the whole
        session) so the purchase's stock effect is all-or-nothing without
        disturbing anything else the caller's session may already hold
        pending (e.g. in tests, fixture setup done earlier on the same
        session/transaction). We then commit the needs_refund status +
        audit row explicitly so they survive even though the caller (the
        endpoint, via get_db) will roll back the rest of the request when
        the HTTPException below propagates.
        """
        for prior_ticket_type_id, prior_quantity in decremented_so_far or []:
            await self.ticket_type_repo.increment_available(prior_ticket_type_id, prior_quantity)

        await txn_repo.update_status(reference, "needs_refund", paystack_data)
        await self.audit_repo.log_action(
            action_type="TICKET_PAYMENT_NEEDS_REFUND",
            entity_type="TicketTransaction",
            entity_id=transaction_id,
            user_id=user_id,
            changes={"reason": reason, "ticket_type_id": str(ticket_type_id)},
        )
        await txn_repo.session.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

    async def fulfill_paid_purchase(
        self, reference: str, paystack_data: dict, txn_repo: "TicketTransactionRepository"
    ) -> TicketPurchaseResponse:
        txn = await txn_repo.get_by_reference(reference)
        if not txn:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")
        if txn.status == "success":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This payment has already been fulfilled")
        if txn.status in ("failed", "needs_refund"):
            # Authoritative terminal-state guard — this method is also called
            # directly from the webhook branch (Task 4), which never goes
            # through the verify-and-purchase endpoint's own checks, so this
            # can't just live in the endpoint.
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment failed — please try again")

        # Capture the scalars we need off `txn` up front rather than reading
        # `txn.*` again after any of the commits below.
        txn_id = txn.transaction_id
        txn_user_id = txn.user_id
        txn_event_id = txn.event_id
        txn_amount = txn.amount

        event = await self.event_repo.get_with_ticket_types(txn_event_id)
        if not event or event.status != "published":
            await txn_repo.update_status(reference, "failed", paystack_data)
            await txn_repo.session.commit()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Event is no longer available")

        now = datetime.now(timezone.utc)
        type_map = {tt.ticket_type_id: tt for tt in event.ticket_types}
        ticket_items = []
        decremented: List["tuple[UUID, int]"] = []

        for item in txn.items:
            ticket_type_id = UUID(item["ticket_type_id"])
            quantity = item["quantity"]
            ticket_type = type_map.get(ticket_type_id)
            if (
                not ticket_type
                or ticket_type.status != "active"
                or (ticket_type.sales_start_datetime and now < ticket_type.sales_start_datetime)
                or (ticket_type.sales_end_datetime and now > ticket_type.sales_end_datetime)
            ):
                await self._mark_needs_refund(
                    txn_repo, reference, paystack_data,
                    transaction_id=txn_id, user_id=txn_user_id,
                    reason="ticket_type_unavailable", ticket_type_id=ticket_type_id,
                    detail="Payment succeeded but a ticket type became unavailable. Your payment will be refunded — contact support if you don't hear back within 24 hours.",
                    decremented_so_far=decremented,
                )

            success = await self.ticket_type_repo.decrement_available(ticket_type_id, quantity)
            if not success:
                await self._mark_needs_refund(
                    txn_repo, reference, paystack_data,
                    transaction_id=txn_id, user_id=txn_user_id,
                    reason="sold_out", ticket_type_id=ticket_type_id,
                    detail="Payment succeeded but this ticket type sold out before it could be issued. Your payment will be refunded — contact support if you don't hear back within 24 hours.",
                    decremented_so_far=decremented,
                )
            decremented.append((ticket_type_id, quantity))
            ticket_items.append((ticket_type, quantity))

        purchase = await self.purchase_repo.create({
            "user_id": txn_user_id,
            "event_id": txn_event_id,
            "total_amount": txn_amount,
            "payment_status": "completed",
        })

        tickets = []
        for ticket_type, quantity in ticket_items:
            for _ in range(quantity):
                ticket_code = generate_secure_token(16)
                qr_data = json.dumps({
                    "ticket_code": ticket_code,
                    "event_id": str(txn_event_id),
                    "ticket_type": ticket_type.type_name,
                })
                ticket = await self.ticket_repo.create({
                    "ticket_code": ticket_code,
                    "event_id": txn_event_id,
                    "ticket_type_id": ticket_type.ticket_type_id,
                    "user_id": txn_user_id,
                    "purchase_id": purchase.purchase_id,
                    "qr_code_data": qr_data,
                })
                tickets.append(ticket)

        await self.audit_repo.log_action(
            action_type="TICKET_PURCHASE",
            entity_type="Event",
            entity_id=txn_event_id,
            user_id=txn_user_id,
            changes={"total_amount": str(txn_amount), "ticket_count": len(tickets), "reference": reference},
        )

        buyer = await self.user_repo.get_by_id(txn_user_id, id_field="user_id")
        if buyer and buyer.email:
            subject, html = ticket_confirmation_email(event.title, len(tickets))
            send_email(buyer.email, subject, html)

        await txn_repo.update_status(reference, "success", paystack_data)

        return TicketPurchaseResponse(
            purchase_id=purchase.purchase_id,
            event_id=txn_event_id,
            total_amount=txn_amount,
            payment_status=purchase.payment_status,
            tickets=[
                TicketResponse(
                    ticket_id=t.ticket_id,
                    ticket_code=t.ticket_code,
                    event_id=t.event_id,
                    ticket_type_id=t.ticket_type_id,
                    ticket_status=t.ticket_status,
                    purchase_date=t.purchase_date,
                )
                for t in tickets
            ],
            purchased_at=purchase.purchased_at,
        )

    async def validate_ticket(
        self,
        ticket_code: str,
        scanned_by: UUID,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> TicketValidationResponse:
        ticket = await self.ticket_repo.get_by_ticket_code(ticket_code)
        if not ticket:
            return TicketValidationResponse(
                valid=False,
                message="Ticket not found",
            )

        if ticket.ticket_status == "used":
            return TicketValidationResponse(
                valid=False,
                message="Ticket has already been used",
                ticket=TicketResponse(
                    ticket_id=ticket.ticket_id,
                    ticket_code=ticket.ticket_code,
                    event_id=ticket.event_id,
                    ticket_type_id=ticket.ticket_type_id,
                    ticket_status=ticket.ticket_status,
                    purchase_date=ticket.purchase_date,
                    used_at=ticket.used_at,
                ),
            )

        if ticket.ticket_status == "cancelled":
            return TicketValidationResponse(
                valid=False,
                message="Ticket has been cancelled",
            )

        # Mark as used
        updated = await self.ticket_repo.mark_as_used(
            ticket.ticket_id, scanned_by
        )

        # Audit log
        await self.audit_repo.log_action(
            action_type="TICKET_SCANNED",
            entity_type="Ticket",
            entity_id=ticket.ticket_id,
            user_id=scanned_by,
            ip_address=ip,
            user_agent=user_agent,
        )

        return TicketValidationResponse(
            valid=True,
            message="Ticket is valid. Entry granted.",
            ticket=TicketResponse(
                ticket_id=updated.ticket_id,
                ticket_code=updated.ticket_code,
                event_id=updated.event_id,
                ticket_type_id=updated.ticket_type_id,
                ticket_status=updated.ticket_status,
                purchase_date=updated.purchase_date,
                used_at=updated.used_at,
            ),
            attendee_name=updated.user.full_name if updated.user else None,
            event_title=updated.event.title if updated.event else None,
            ticket_type=updated.ticket_type.type_name if updated.ticket_type else None,
        )

    async def get_user_tickets(
        self, user_id: UUID, skip: int = 0, limit: int = 20
    ) -> List[TicketResponse]:
        tickets = await self.ticket_repo.get_user_tickets(user_id, skip, limit)
        return [
            TicketResponse(
                ticket_id=t.ticket_id,
                ticket_code=t.ticket_code,
                event_id=t.event_id,
                ticket_type_id=t.ticket_type_id,
                ticket_status=t.ticket_status,
                purchase_date=t.purchase_date,
                used_at=t.used_at,
            )
            for t in tickets
        ]

    async def cancel_ticket(
        self, ticket_id: UUID, user_id: UUID
    ) -> TicketResponse:
        ticket = await self.ticket_repo.get_by_id(ticket_id, id_field="ticket_id")
        if not ticket:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Ticket not found",
            )
        if ticket.user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only cancel your own tickets",
            )
        if ticket.ticket_status != "valid":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only valid tickets can be cancelled",
            )

        ticket.ticket_status = "cancelled"
        await self.ticket_repo.session.flush()
        await self.ticket_repo.session.refresh(ticket)

        return TicketResponse(
            ticket_id=ticket.ticket_id,
            ticket_code=ticket.ticket_code,
            event_id=ticket.event_id,
            ticket_type_id=ticket.ticket_type_id,
            ticket_status=ticket.ticket_status,
            purchase_date=ticket.purchase_date,
            used_at=ticket.used_at,
        )
