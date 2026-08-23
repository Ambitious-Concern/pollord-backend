import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException, status

from app.core.security import create_ticket_download_token, generate_secure_token
from app.models.event import Event
from app.models.ticket import Ticket, TicketPurchase, is_owned_by
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.event_repository import EventRepository, TicketTypeRepository
from app.repositories.ticket_repository import TicketPurchaseRepository, TicketRepository
from app.repositories.ticket_transaction_repository import TicketTransactionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.ticket import (
    AdminTicketPurchaseListResponse,
    AdminTicketPurchaseResponse,
    ManualAttendeeRequest,
    ManualAttendeeResponse,
    ManualPaymentLookupResponse,
    ResendTicketEmailResponse,
    TicketDetailResponse,
    TicketPurchaseRequest,
    TicketPurchaseResponse,
    TicketResponse,
    TicketSaleResponse,
    TicketValidationResponse,
)
from app.services.email_service import (
    guest_ticket_confirmation_email,
    send_email,
    ticket_confirmation_email,
)

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
        user_id: Optional[UUID],
        data: TicketPurchaseRequest,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        guest_name: Optional[str] = None,
        guest_email: Optional[str] = None,
        guest_phone: Optional[str] = None,
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

            # Check per-user (or per-guest-email) limit
            if user_id is not None:
                existing_count = await self.ticket_repo.count_user_tickets_for_type(
                    user_id, item.ticket_type_id
                )
            else:
                existing_count = await self.ticket_repo.count_guest_tickets_for_type(
                    guest_email, item.ticket_type_id
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

            total_amount += Decimal(str(ticket_type.price)) * item.quantity
            ticket_items.append((ticket_type, item.quantity))

        if total_amount > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This purchase requires payment — use POST /tickets/initiate-payment instead",
            )

        # 3. Create purchase record
        payment_status = "completed"
        purchase = await self.purchase_repo.create(
            {
                "user_id": user_id,
                "guest_name": guest_name,
                "guest_email": guest_email,
                "guest_phone": guest_phone,
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
                        "guest_name": guest_name,
                        "guest_email": guest_email,
                        "guest_phone": guest_phone,
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

        await self._send_purchase_confirmation(
            event.title, tickets, user_id, guest_email, purchase=purchase
        )

        return TicketPurchaseResponse(
            purchase_id=purchase.purchase_id,
            event_id=data.event_id,
            total_amount=total_amount,
            payment_status=purchase.payment_status,
            tickets=[self._to_ticket_response(t) for t in tickets],
            purchased_at=purchase.purchased_at,
        )

    def _to_ticket_response(self, t: Ticket) -> TicketResponse:
        return TicketResponse(
            ticket_id=t.ticket_id,
            ticket_code=t.ticket_code,
            event_id=t.event_id,
            ticket_type_id=t.ticket_type_id,
            ticket_status=t.ticket_status,
            purchase_date=t.purchase_date,
            used_at=t.used_at,
            download_token=create_ticket_download_token(str(t.ticket_id)) if t.user_id is None else None,
        )

    async def _build_confirmation_email(
        self,
        event_title: str,
        tickets: List[Ticket],
        user_id: Optional[UUID],
        guest_email: Optional[str],
    ) -> Optional[tuple[str, str, str]]:
        """Build (recipient, subject, html) for a purchase's ticket email.

        Registered buyers get a My-Tickets link; guests get one download link
        per ticket, since they have no account to sign in to. Returns None
        when there is no address to send to. Shared by the purchase flow and
        the admin resend so a resent email is byte-for-byte the original.
        """
        if user_id is not None:
            buyer = await self.user_repo.get_by_id(user_id, id_field="user_id")
            if not buyer or not buyer.email:
                return None
            subject, html = ticket_confirmation_email(event_title, len(tickets))
            return buyer.email, subject, html

        if guest_email:
            from app.core.config import settings

            links = [
                f"{settings.PUBLIC_BASE_URL}/api/v1/tickets/public/download/{create_ticket_download_token(str(t.ticket_id))}"
                for t in tickets
            ]
            subject, html = guest_ticket_confirmation_email(event_title, len(tickets), links)
            return guest_email, subject, html

        return None

    @staticmethod
    def _record_email_attempt(
        purchase: Optional[TicketPurchase], recipient: Optional[str], sent: bool
    ) -> None:
        """Stamp the delivery outcome onto the purchase.

        send_email swallows its own failures, so without this a bounced or
        refused ticket email leaves no trace an admin could search for.
        """
        if purchase is None:
            return
        purchase.confirmation_email_status = "sent" if sent else "failed"
        purchase.confirmation_email_attempted_at = datetime.now(timezone.utc)
        purchase.confirmation_email_to = recipient

    async def _send_purchase_confirmation(
        self,
        event_title: str,
        tickets: List[Ticket],
        user_id: Optional[UUID],
        guest_email: Optional[str],
        purchase: Optional[TicketPurchase] = None,
    ) -> bool:
        """Best-effort — never blocks the purchase."""
        message = await self._build_confirmation_email(
            event_title, tickets, user_id, guest_email
        )
        if message is None:
            self._record_email_attempt(purchase, None, False)
            return False

        recipient, subject, html = message
        sent = send_email(recipient, subject, html)
        self._record_email_attempt(purchase, recipient, sent)
        return sent

    async def _find_existing_purchase_for_reference(
        self, reference: str
    ) -> Optional[TicketPurchase]:
        from sqlalchemy import select

        result = await self.purchase_repo.session.execute(
            select(TicketPurchase).where(TicketPurchase.payment_reference == reference)
        )
        return result.scalars().first()

    async def inspect_payment_reference(
        self, reference: str, paystack, txn_repo: "TicketTransactionRepository"
    ) -> "ManualPaymentLookupResponse":
        """Look up a Paystack reference without changing anything.

        Backs the form's Verify step, so an admin sees the real amount and
        payer before issuing anything, and is told up front if this payment
        has already produced tickets.
        """
        ps_data = await paystack.verify_transaction(reference)

        existing = await self._find_existing_purchase_for_reference(reference)
        txn = await txn_repo.get_by_reference(reference)
        already_fulfilled = existing is not None or (
            txn is not None and txn.status == "success"
        )

        customer = ps_data.get("customer") or {}
        return ManualPaymentLookupResponse(
            reference=reference,
            paystack_status=ps_data.get("status", "unknown"),
            amount=Decimal(str(ps_data.get("amount", 0))) / 100,
            currency=ps_data.get("currency", "GHS"),
            paid_at=ps_data.get("paid_at"),
            customer_email=customer.get("email"),
            already_fulfilled=already_fulfilled,
            existing_purchase_id=existing.purchase_id if existing else None,
        )

    async def issue_ticket_for_untracked_payment(
        self,
        event_id: UUID,
        data: "ManualAttendeeRequest",
        actor_user_id: UUID,
        paystack,
        txn_repo: "TicketTransactionRepository",
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> "ManualAttendeeResponse":
        """Issue tickets for a Paystack payment the system never captured.

        For buyers who really did pay but whose checkout never completed on
        our side — so the amount is recorded as genuine revenue, not a comp.
        The reference is re-verified against Paystack here rather than trusted
        from the request: the client already saw a verify result, but nothing
        stops a caller posting straight to this endpoint.
        """
        event = await self.event_repo.get_with_ticket_types(event_id)
        if not event:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Event not found"
            )

        ticket_type = next(
            (
                tt
                for tt in event.ticket_types
                if tt.ticket_type_id == data.ticket_type_id
            ),
            None,
        )
        if not ticket_type:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="That ticket type does not belong to this event",
            )

        existing = await self._find_existing_purchase_for_reference(data.reference)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This Paystack reference has already been used to issue "
                    "tickets. Find the purchase in Ticket Purchases and resend "
                    "its email instead of issuing again."
                ),
            )

        ps_data = await paystack.verify_transaction(data.reference)
        if ps_data.get("status") != "success":
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=(
                    f"Paystack reports this payment as "
                    f"'{ps_data.get('status', 'unknown')}', not success. No "
                    "tickets were issued."
                ),
            )

        txn = await txn_repo.get_by_reference(data.reference)
        if txn is not None and txn.status == "success":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This payment has already been fulfilled",
            )

        # Real money changed hands, so record Paystack's figure rather than
        # the ticket price — they may have been charged something else.
        amount_paid = Decimal(str(ps_data.get("amount", 0))) / 100

        if not await self.ticket_type_repo.decrement_available(
            data.ticket_type_id, data.quantity
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"'{ticket_type.type_name}' has no stock left. Increase its "
                    "quantity before issuing, so capacity stays truthful."
                ),
            )

        # Attach to their account when one exists, so the tickets show up in
        # My Tickets rather than only as a guest download link.
        buyer = await self.user_repo.get_by_email(data.email)
        buyer_id = buyer.user_id if buyer else None

        purchase = await self.purchase_repo.create(
            {
                "user_id": buyer_id,
                "guest_name": None if buyer_id else data.name,
                "guest_email": None if buyer_id else data.email,
                "guest_phone": None if buyer_id else data.phone,
                "event_id": event_id,
                "total_amount": amount_paid,
                "payment_status": "completed",
                "payment_method": "paystack",
                "payment_reference": data.reference,
            }
        )

        tickets = []
        for _ in range(data.quantity):
            ticket_code = generate_secure_token(16)
            qr_data = json.dumps(
                {
                    "ticket_code": ticket_code,
                    "event_id": str(event_id),
                    "ticket_type": ticket_type.type_name,
                }
            )
            tickets.append(
                await self.ticket_repo.create(
                    {
                        "ticket_code": ticket_code,
                        "event_id": event_id,
                        "ticket_type_id": ticket_type.ticket_type_id,
                        "user_id": buyer_id,
                        "guest_name": None if buyer_id else data.name,
                        "guest_email": None if buyer_id else data.email,
                        "guest_phone": None if buyer_id else data.phone,
                        "purchase_id": purchase.purchase_id,
                        "qr_code_data": qr_data,
                    }
                )
            )

        # Close the door on the original webhook: if Paystack ever retries it,
        # fulfill_paid_purchase now sees "success" and refuses, instead of
        # issuing a duplicate set of tickets for the same payment.
        if txn is not None:
            await txn_repo.update_status(data.reference, "success", ps_data)

        await self.audit_repo.log_action(
            action_type="TICKET_ISSUED_MANUALLY",
            entity_type="TicketPurchase",
            entity_id=purchase.purchase_id,
            user_id=actor_user_id,
            changes={
                "reference": data.reference,
                "event_id": str(event_id),
                "ticket_type": ticket_type.type_name,
                "quantity": data.quantity,
                "amount": str(amount_paid),
                "email": data.email,
                "linked_existing_transaction": txn is not None,
            },
            ip_address=ip,
            user_agent=user_agent,
        )

        email_sent = await self._send_purchase_confirmation(
            event.title,
            tickets,
            buyer_id,
            data.email if not buyer_id else None,
            purchase=purchase,
        )

        return ManualAttendeeResponse(
            purchase_id=purchase.purchase_id,
            event_id=event_id,
            ticket_count=len(tickets),
            amount=amount_paid,
            email=data.email,
            email_sent=email_sent,
            message=(
                f"Issued {len(tickets)} ticket(s) to {data.email}."
                + (
                    ""
                    if email_sent
                    else " The ticket email could not be sent — resend it from "
                    "Ticket Purchases once mail is working."
                )
            ),
        )

    async def resend_ticket_email_for_sale(
        self,
        ticket_id: UUID,
        actor: User,
        override_email: Optional[str] = None,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> ResendTicketEmailResponse:
        """Organizer-facing resend, addressed by ticket rather than purchase.

        The Tickets Sold table is one row per issued ticket, so that's the id
        the caller has. The confirmation email covers the whole order, so we
        resolve up to the purchase and send that — clicking any row of a
        3-ticket order sends the same email.
        """
        ticket = await self.ticket_repo.get_by_id(ticket_id, id_field="ticket_id")
        if not ticket:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Ticket not found",
            )

        event = await self.event_repo.get_by_id(ticket.event_id, id_field="event_id")
        # Same rule as the rest of the event routes: the creator, or any
        # System Administrator.
        actor_roles = [ur.role.role_name for ur in actor.user_roles]
        if not event or (
            event.created_by != actor.user_id
            and "System Administrator" not in actor_roles
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this event",
            )

        return await self.resend_purchase_confirmation(
            purchase_id=ticket.purchase_id,
            actor_user_id=actor.user_id,
            override_email=override_email,
            ip=ip,
            user_agent=user_agent,
        )

    async def resend_purchase_confirmation(
        self,
        purchase_id: UUID,
        actor_user_id: UUID,
        override_email: Optional[str] = None,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> ResendTicketEmailResponse:
        """Re-send a purchase's ticket email, optionally to a corrected address.

        Callers must authorize first — this does no access checking of its own.

        Synchronous on purpose: whoever triggers this is watching a customer
        wait, and needs to know whether the send actually succeeded rather
        than that it was queued.
        """
        purchase = await self.purchase_repo.get_with_details(purchase_id)
        if not purchase:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Purchase not found",
            )

        if not purchase.tickets:
            # Almost always a payment whose Paystack webhook never landed, so
            # fulfilment never ran. Resending can't help — there is nothing to
            # send — and saying so points support at the real problem.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This purchase has no issued tickets, so there is nothing to "
                    "resend. The payment was most likely never fulfilled — check "
                    "the ticket transaction for this reference."
                ),
            )

        message = await self._build_confirmation_email(
            purchase.event.title if purchase.event else "",
            purchase.tickets,
            purchase.user_id,
            purchase.guest_email,
        )
        if message is None and not override_email:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This purchase has no email address on record. Supply one to "
                    "send the tickets."
                ),
            )

        if message is None:
            # No address on the purchase, but the admin supplied one. Guests
            # and account holders differ only in which links the body carries;
            # with no user_id we fall back to the guest (download-link) body,
            # which is the one that works without signing in.
            from app.core.config import settings

            links = [
                f"{settings.PUBLIC_BASE_URL}/api/v1/tickets/public/download/{create_ticket_download_token(str(t.ticket_id))}"
                for t in purchase.tickets
            ]
            subject, html = guest_ticket_confirmation_email(
                purchase.event.title if purchase.event else "",
                len(purchase.tickets),
                links,
            )
        else:
            _, subject, html = message

        original_email = message[0] if message else None
        recipient = override_email or original_email

        sent = send_email(recipient, subject, html)
        self._record_email_attempt(purchase, recipient, sent)
        await self.purchase_repo.session.flush()

        await self.audit_repo.log_action(
            action_type="TICKET_EMAIL_RESENT",
            entity_type="TicketPurchase",
            entity_id=purchase_id,
            user_id=actor_user_id,
            changes={
                "email": recipient,
                "overridden": bool(override_email and override_email != original_email),
                "original_email": original_email,
                "ticket_count": len(purchase.tickets),
                "sent": sent,
            },
            ip_address=ip,
            user_agent=user_agent,
        )

        return ResendTicketEmailResponse(
            purchase_id=purchase_id,
            sent=sent,
            email=recipient,
            ticket_count=len(purchase.tickets),
            message=(
                f"Ticket email sent to {recipient}."
                if sent
                else (
                    f"Could not send to {recipient} — the mail server rejected the "
                    "message or is unreachable. Check the server logs."
                )
            ),
        )

    async def list_purchases_for_admin(
        self,
        search: Optional[str] = None,
        event_id: Optional[UUID] = None,
        payment_status: Optional[str] = None,
        email_status: Optional[str] = None,
        skip: int = 0,
        limit: int = 50,
    ) -> AdminTicketPurchaseListResponse:
        purchases, total = await self.purchase_repo.search_for_admin(
            search=search,
            event_id=event_id,
            payment_status=payment_status,
            email_status=email_status,
            skip=skip,
            limit=limit,
        )

        return AdminTicketPurchaseListResponse(
            items=[
                AdminTicketPurchaseResponse(
                    purchase_id=p.purchase_id,
                    event_id=p.event_id,
                    event_title=p.event.title if p.event else "",
                    buyer_name=(p.user.full_name if p.user else p.guest_name) or "",
                    buyer_email=(p.user.email if p.user else p.guest_email) or "",
                    is_guest=p.user_id is None,
                    ticket_count=len(p.tickets),
                    total_amount=p.total_amount,
                    payment_status=p.payment_status,
                    payment_reference=p.payment_reference,
                    purchased_at=p.purchased_at,
                    confirmation_email_status=p.confirmation_email_status or "unknown",
                    confirmation_email_attempted_at=p.confirmation_email_attempted_at,
                    confirmation_email_to=p.confirmation_email_to,
                )
                for p in purchases
            ],
            total=total,
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
        txn_guest_name = txn.guest_name
        txn_guest_email = txn.guest_email
        txn_guest_phone = txn.guest_phone
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
            "guest_name": txn_guest_name,
            "guest_email": txn_guest_email,
            "guest_phone": txn_guest_phone,
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
                    "guest_name": txn_guest_name,
                    "guest_email": txn_guest_email,
                    "guest_phone": txn_guest_phone,
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

        await self._send_purchase_confirmation(
            event.title, tickets, txn_user_id, txn_guest_email, purchase=purchase
        )

        await txn_repo.update_status(reference, "success", paystack_data)

        return TicketPurchaseResponse(
            purchase_id=purchase.purchase_id,
            event_id=txn_event_id,
            total_amount=txn_amount,
            payment_status=purchase.payment_status,
            tickets=[self._to_ticket_response(t) for t in tickets],
            purchased_at=purchase.purchased_at,
        )

    async def validate_ticket(
        self,
        ticket_code: str,
        scanned_by: Optional[UUID],
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        expected_event_id: Optional[UUID] = None,
    ) -> TicketValidationResponse:
        ticket = await self.ticket_repo.get_by_ticket_code(ticket_code)
        if not ticket:
            return TicketValidationResponse(
                valid=False,
                message="Ticket not found",
            )

        if expected_event_id and ticket.event_id != expected_event_id:
            # Deliberately vague — a scan link scoped to one event shouldn't
            # confirm that a mismatched code is even a *valid* ticket for
            # some other event.
            return TicketValidationResponse(
                valid=False,
                message="This ticket is not valid for this event",
            )

        # Checked here rather than in the endpoints because both the public
        # link scanner and the signed-in organizer scanner come through this
        # method — putting it in one route would leave the other open.
        if ticket.event is not None and not ticket.event.scan_enabled:
            return TicketValidationResponse(
                valid=False,
                message=(
                    "Check-in is turned off for this event. The organizer can "
                    "re-enable it from the event page."
                ),
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
            attendee_name=(updated.user.full_name if updated.user else updated.guest_name),
            event_title=updated.event.title if updated.event else None,
            ticket_type=updated.ticket_type.type_name if updated.ticket_type else None,
        )

    async def get_user_tickets(
        self,
        user_id: UUID,
        email: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> List[TicketDetailResponse]:
        tickets = await self.ticket_repo.get_user_tickets(user_id, email, skip, limit)
        return [
            TicketDetailResponse(
                ticket_id=t.ticket_id,
                ticket_code=t.ticket_code,
                event_id=t.event_id,
                ticket_type_id=t.ticket_type_id,
                ticket_status=t.ticket_status,
                purchase_date=t.purchase_date,
                used_at=t.used_at,
                event_title=t.event.title if t.event else "",
                ticket_type_name=t.ticket_type.type_name if t.ticket_type else "",
                attendee_name=(t.user.full_name if t.user else t.guest_name) or "",
            )
            for t in tickets
        ]

    async def get_organizer_ticket_sales(
        self,
        organizer_ids: List[UUID],
        event_id: Optional[UUID] = None,
        skip: int = 0,
        limit: int = 50,
    ) -> List[TicketSaleResponse]:
        """`organizer_ids` is the caller plus their organization teammates —
        sales belong to the organization, not just whoever created the event."""
        tickets = await self.ticket_repo.get_organizer_tickets(organizer_ids, event_id, skip, limit)
        return [
            TicketSaleResponse(
                ticket_id=t.ticket_id,
                ticket_code=t.ticket_code,
                event_id=t.event_id,
                ticket_type_id=t.ticket_type_id,
                ticket_status=t.ticket_status,
                purchase_date=t.purchase_date,
                used_at=t.used_at,
                event_title=t.event.title if t.event else "",
                ticket_type_name=t.ticket_type.type_name if t.ticket_type else "",
                attendee_name=(t.user.full_name if t.user else t.guest_name) or "",
                attendee_email=(t.user.email if t.user else t.guest_email) or "",
                amount=t.ticket_type.price if t.ticket_type else Decimal("0"),
            )
            for t in tickets
        ]

    async def cancel_ticket(
        self, ticket_id: UUID, user: User
    ) -> TicketResponse:
        ticket = await self.ticket_repo.get_by_id(ticket_id, id_field="ticket_id")
        if not ticket:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Ticket not found",
            )
        if not is_owned_by(ticket, user):
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
