import secrets
from datetime import date, time
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_current_active_user, require_roles
from app.core.security import decode_token
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.event import Event, TicketType
from app.models.organization import Organization
from app.models.ticket import Ticket, TicketPurchase, is_owned_by
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.event_repository import EventRepository, TicketTypeRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.ticket_repository import TicketPurchaseRepository, TicketRepository
from app.repositories.ticket_transaction_repository import TicketTransactionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.ticket import (
    GuestTicketPurchaseRequest,
    PublicTicketValidation,
    ResendTicketEmailRequest,
    ResendTicketEmailResponse,
    ScanInfoResponse,
    TicketDetailResponse,
    TicketPaymentInitResponse,
    TicketPurchaseRequest,
    TicketPurchaseResponse,
    TicketResponse,
    TicketSaleResponse,
    TicketValidation,
    TicketValidationResponse,
    VerifyAndPurchaseRequest,
)
from app.services.paystack_service import PaystackService
from app.services.ticketing_service import TicketingService
from app.utils.pdf_generator import generate_ticket_pdf
from app.utils.qr_code import generate_qr_code

router = APIRouter(prefix="/tickets", tags=["Tickets"])


async def _fetch_image_bytes(url: Optional[str]) -> Optional[bytes]:
    """Best-effort fetch of an event banner / org logo for embedding in the ticket PDF.
    Never raises — a missing/slow image just falls back to the placeholder box."""
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(url)
            res.raise_for_status()
            return res.content
    except Exception:
        return None


async def _fetch_organizer_logo_bytes(db: AsyncSession, organizer_id: Optional[UUID]) -> Optional[bytes]:
    """The ticket PDF footer shows the organizing event's own org logo alongside
    Pollord's. An organizer owns at most one org in this app, so we just take
    the first one they own — there's no direct event->org link to follow."""
    if not organizer_id:
        return None
    orgs = await OrganizationRepository(Organization, db).get_by_owner(organizer_id)
    if not orgs:
        return None
    return await _fetch_image_bytes(orgs[0].logo_url)


def _get_ticketing_service(db: AsyncSession) -> TicketingService:
    return TicketingService(
        event_repo=EventRepository(Event, db),
        ticket_type_repo=TicketTypeRepository(TicketType, db),
        ticket_repo=TicketRepository(Ticket, db),
        purchase_repo=TicketPurchaseRepository(TicketPurchase, db),
        audit_repo=AuditLogRepository(AuditLog, db),
        user_repo=UserRepository(User, db),
    )


@router.post("/purchase", response_model=TicketPurchaseResponse, status_code=201)
async def purchase_tickets(
    data: TicketPurchaseRequest,
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    service = _get_ticketing_service(db)
    return await service.purchase_tickets(
        user_id=current_user.user_id,
        data=data,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.post("/initiate-payment", response_model=TicketPaymentInitResponse, status_code=201)
async def initiate_ticket_payment(
    data: TicketPurchaseRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    from datetime import datetime, timezone
    from fastapi import HTTPException, status as http_status

    event_repo = EventRepository(Event, db)
    event = await event_repo.get_with_ticket_types(data.event_id)
    if not event:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Event not found")
    if event.status != "published":
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Event is not available for ticket purchase",
        )

    now = datetime.now(timezone.utc)
    total_amount = Decimal("0.00")
    type_map = {tt.ticket_type_id: tt for tt in event.ticket_types}

    for item in data.items:
        ticket_type = type_map.get(item.ticket_type_id)
        if not ticket_type:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=f"Ticket type {item.ticket_type_id} not found for this event",
            )
        if ticket_type.status != "active":
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=f"Ticket type '{ticket_type.type_name}' is not available",
            )
        if ticket_type.sales_start_datetime and now < ticket_type.sales_start_datetime:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=f"Sales for '{ticket_type.type_name}' have not started yet",
            )
        if ticket_type.sales_end_datetime and now > ticket_type.sales_end_datetime:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=f"Sales for '{ticket_type.type_name}' have ended",
            )
        ticket_repo = TicketRepository(Ticket, db)
        existing_count = await ticket_repo.count_user_tickets_for_type(
            current_user.user_id, item.ticket_type_id
        )
        if existing_count + item.quantity > ticket_type.max_per_user:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=f"Maximum {ticket_type.max_per_user} tickets per user for '{ticket_type.type_name}'",
            )
        total_amount += Decimal(str(ticket_type.price)) * item.quantity

    if total_amount <= 0:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="This purchase is free. Use POST /tickets/purchase instead",
        )

    reference = f"ticket_{secrets.token_urlsafe(16)}"
    txn_repo = TicketTransactionRepository(db)
    await txn_repo.create({
        "reference": reference,
        "user_id": current_user.user_id,
        "event_id": data.event_id,
        "items": [{"ticket_type_id": str(i.ticket_type_id), "quantity": i.quantity} for i in data.items],
        "amount": total_amount,
        "currency": "GHS",
        "status": "pending",
    })

    paystack = PaystackService(settings.PAYSTACK_SECRET_KEY)
    ps_data = await paystack.initialize_transaction(
        email=current_user.email,
        amount=int(total_amount * 100),  # pesewas
        reference=reference,
        currency="GHS",
        metadata={"event_id": str(data.event_id), "event_title": event.title},
    )

    return TicketPaymentInitResponse(
        reference=reference,
        access_code=ps_data["access_code"],
        public_key=settings.PAYSTACK_PUBLIC_KEY,
        amount=total_amount,
        currency="GHS",
    )


@router.post("/verify-and-purchase", response_model=TicketPurchaseResponse, status_code=201)
async def verify_and_purchase_tickets(
    data: VerifyAndPurchaseRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    from fastapi import HTTPException, status as http_status

    txn_repo = TicketTransactionRepository(db)
    txn = await txn_repo.get_by_reference(data.reference)
    if not txn:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Transaction not found")
    # NOTE: these two checks are a non-authoritative optimization only — they
    # exist purely to skip an unnecessary Paystack API call for a
    # transaction that's already terminal. The authoritative version of this
    # guard lives in TicketingService.fulfill_paid_purchase, since that
    # method is also called directly from the webhook branch, which never
    # goes through this endpoint.
    if txn.status == "success":
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail="This payment has already been fulfilled")
    if txn.status in ("failed", "needs_refund"):
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Payment failed, please try again")

    paystack = PaystackService(settings.PAYSTACK_SECRET_KEY)
    ps_data = await paystack.verify_transaction(data.reference)

    if ps_data.get("status") != "success":
        await txn_repo.update_status(data.reference, "failed", ps_data)
        # Commit explicitly: get_db's rollback-on-exception would otherwise
        # discard this status update along with the HTTPException we're
        # about to raise, leaving the transaction stuck at "pending" and a
        # retry able to re-run this same path indefinitely.
        await db.commit()
        raise HTTPException(status_code=http_status.HTTP_402_PAYMENT_REQUIRED, detail="Payment was not successful")

    if ps_data.get("amount", 0) < int(txn.amount * 100):
        await txn_repo.update_status(data.reference, "failed", ps_data)
        await db.commit()
        raise HTTPException(
            status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
            detail="Payment amount does not match the ticket price",
        )

    service = _get_ticketing_service(db)
    return await service.fulfill_paid_purchase(data.reference, ps_data, txn_repo)



# =========================================================================
# Public ticket endpoints — no authentication required
# Guest identity (name/email/phone) is carried in the request body instead.
# =========================================================================


@router.post("/public/purchase", response_model=TicketPurchaseResponse, status_code=201)
async def purchase_tickets_guest(
    data: GuestTicketPurchaseRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Free-ticket checkout for a guest with no account."""
    service = _get_ticketing_service(db)
    return await service.purchase_tickets(
        user_id=None,
        data=data,
        guest_name=data.guest_name,
        guest_email=data.guest_email,
        guest_phone=data.guest_phone,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.post("/public/initiate-payment", response_model=TicketPaymentInitResponse, status_code=201)
async def initiate_ticket_payment_guest(
    data: GuestTicketPurchaseRequest,
    db: AsyncSession = Depends(get_db),
):
    """Step 1 of paid guest checkout — mirrors initiate_ticket_payment but
    takes guest identity from the request body instead of a Bearer token."""
    from datetime import datetime, timezone
    from fastapi import HTTPException, status as http_status

    event_repo = EventRepository(Event, db)
    event = await event_repo.get_with_ticket_types(data.event_id)
    if not event:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Event not found")
    if event.status != "published":
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Event is not available for ticket purchase",
        )

    now = datetime.now(timezone.utc)
    total_amount = Decimal("0.00")
    type_map = {tt.ticket_type_id: tt for tt in event.ticket_types}

    for item in data.items:
        ticket_type = type_map.get(item.ticket_type_id)
        if not ticket_type:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=f"Ticket type {item.ticket_type_id} not found for this event",
            )
        if ticket_type.status != "active":
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=f"Ticket type '{ticket_type.type_name}' is not available",
            )
        if ticket_type.sales_start_datetime and now < ticket_type.sales_start_datetime:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=f"Sales for '{ticket_type.type_name}' have not started yet",
            )
        if ticket_type.sales_end_datetime and now > ticket_type.sales_end_datetime:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=f"Sales for '{ticket_type.type_name}' have ended",
            )
        ticket_repo = TicketRepository(Ticket, db)
        existing_count = await ticket_repo.count_guest_tickets_for_type(
            data.guest_email, item.ticket_type_id
        )
        if existing_count + item.quantity > ticket_type.max_per_user:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=f"Maximum {ticket_type.max_per_user} tickets per user for '{ticket_type.type_name}'",
            )
        total_amount += Decimal(str(ticket_type.price)) * item.quantity

    if total_amount <= 0:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="This purchase is free. Use POST /tickets/public/purchase instead",
        )

    reference = f"ticket_{secrets.token_urlsafe(16)}"
    txn_repo = TicketTransactionRepository(db)
    await txn_repo.create({
        "reference": reference,
        "user_id": None,
        "guest_name": data.guest_name,
        "guest_email": data.guest_email,
        "guest_phone": data.guest_phone,
        "event_id": data.event_id,
        "items": [{"ticket_type_id": str(i.ticket_type_id), "quantity": i.quantity} for i in data.items],
        "amount": total_amount,
        "currency": "GHS",
        "status": "pending",
    })

    paystack = PaystackService(settings.PAYSTACK_SECRET_KEY)
    ps_data = await paystack.initialize_transaction(
        email=data.guest_email,
        amount=int(total_amount * 100),  # pesewas
        reference=reference,
        currency="GHS",
        metadata={"event_id": str(data.event_id), "event_title": event.title, "guest": True},
    )

    return TicketPaymentInitResponse(
        reference=reference,
        access_code=ps_data["access_code"],
        public_key=settings.PAYSTACK_PUBLIC_KEY,
        amount=total_amount,
        currency="GHS",
    )


@router.post("/public/verify-and-purchase", response_model=TicketPurchaseResponse, status_code=201)
async def verify_and_purchase_tickets_guest(
    data: VerifyAndPurchaseRequest,
    db: AsyncSession = Depends(get_db),
):
    """Step 2 of paid guest checkout. `fulfill_paid_purchase` derives
    everything it needs from the stored transaction, so this is otherwise
    identical to the authenticated verify-and-purchase endpoint."""
    from fastapi import HTTPException, status as http_status

    txn_repo = TicketTransactionRepository(db)
    txn = await txn_repo.get_by_reference(data.reference)
    if not txn:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Transaction not found")
    if txn.status == "success":
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail="This payment has already been fulfilled")
    if txn.status in ("failed", "needs_refund"):
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Payment failed, please try again")

    paystack = PaystackService(settings.PAYSTACK_SECRET_KEY)
    ps_data = await paystack.verify_transaction(data.reference)

    if ps_data.get("status") != "success":
        await txn_repo.update_status(data.reference, "failed", ps_data)
        await db.commit()
        raise HTTPException(status_code=http_status.HTTP_402_PAYMENT_REQUIRED, detail="Payment was not successful")

    if ps_data.get("amount", 0) < int(txn.amount * 100):
        await txn_repo.update_status(data.reference, "failed", ps_data)
        await db.commit()
        raise HTTPException(
            status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
            detail="Payment amount does not match the ticket price",
        )

    service = _get_ticketing_service(db)
    return await service.fulfill_paid_purchase(data.reference, ps_data, txn_repo)


@router.get("/public/download/{token}")
async def download_ticket_guest(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """No-auth PDF download for a guest ticket, gated by the signed
    download_token issued at purchase time instead of a Bearer token."""
    from fastapi import HTTPException
    from io import BytesIO

    payload = decode_token(token)
    if not payload or payload.get("type") != "ticket_download":
        raise HTTPException(status_code=404, detail="Invalid or expired download link")

    ticket_id = UUID(payload["sub"])
    ticket_repo = TicketRepository(Ticket, db)
    ticket = await ticket_repo.get_by_id(ticket_id, id_field="ticket_id")
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    qr_bytes = generate_qr_code(ticket.qr_code_data)
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(ticket.event_id, id_field="event_id")
    banner_bytes = await _fetch_image_bytes(event.banner_image_url if event else None)
    org_logo_bytes = await _fetch_organizer_logo_bytes(db, event.created_by if event else None)

    pdf_bytes = generate_ticket_pdf(
        event_title=event.title if event else "Event",
        event_date=event.event_date if event else date.today(),
        event_time=event.event_time if event else time(0, 0),
        location=event.location if event else "",
        ticket_type=ticket.ticket_type.type_name if ticket.ticket_type else "",
        ticket_code=ticket.ticket_code,
        attendee_name=ticket.guest_name or "Guest",
        purchase_date=ticket.purchase_date,
        qr_bytes=qr_bytes,
        banner_image_bytes=banner_bytes,
        org_logo_bytes=org_logo_bytes,
    )

    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="ticket-{ticket.ticket_code}.pdf"'
        },
    )


@router.get("/public/scan-info/{token}", response_model=ScanInfoResponse)
async def get_scan_info(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """Lets the standalone scanner page show event context and detect an
    expired/invalid link before the visitor even opens their camera."""
    from datetime import datetime, timezone
    from fastapi import HTTPException

    payload = decode_token(token)
    if not payload or payload.get("type") != "ticket_scan":
        raise HTTPException(status_code=404, detail="Invalid or expired check-in link")

    event_id = UUID(payload["sub"])
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(event_id, id_field="event_id")
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not event.scan_enabled:
        # A distinct message from "invalid or expired": the organizer turned
        # this off deliberately, and saying so saves them debugging a link
        # that is working exactly as configured.
        raise HTTPException(
            status_code=403,
            detail="Check-in is turned off for this event",
        )

    return ScanInfoResponse(
        event_id=event.event_id,
        event_title=event.title,
        expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
    )


@router.post("/public/validate", response_model=TicketValidationResponse)
async def validate_ticket_guest(
    data: PublicTicketValidation,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """No-auth ticket check-in, gated by an event-scoped scan_token instead
    of a Bearer token — hard-enforces the ticket belongs to that event."""
    from fastapi import HTTPException

    payload = decode_token(data.scan_token)
    if not payload or payload.get("type") != "ticket_scan":
        raise HTTPException(status_code=404, detail="Invalid or expired check-in link")

    service = _get_ticketing_service(db)
    return await service.validate_ticket(
        ticket_code=data.ticket_code,
        scanned_by=None,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        expected_event_id=UUID(payload["sub"]),
    )


@router.get("/my-tickets", response_model=List[TicketDetailResponse])
async def get_my_tickets(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 20,
):
    service = _get_ticketing_service(db)
    return await service.get_user_tickets(
        current_user.user_id, current_user.email, skip, limit
    )


@router.get("/sales", response_model=List[TicketSaleResponse])
async def get_ticket_sales(
    event_id: Optional[UUID] = None,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 50,
):
    """Tickets sold across every event the caller's organization runs,
    optionally narrowed to a single event.

    Scoped to the caller plus their teammates: sales belong to the
    organization, not only to whoever happened to create the event.
    """
    service = _get_ticketing_service(db)
    teammate_ids = await OrganizationRepository(Organization, db).get_teammate_ids(
        current_user.user_id
    )
    return await service.get_organizer_ticket_sales(
        teammate_ids, event_id=event_id, skip=skip, limit=limit
    )


@router.post("/sales/{ticket_id}/resend-email", response_model=ResendTicketEmailResponse)
async def resend_ticket_sale_email(
    ticket_id: UUID,
    data: ResendTicketEmailRequest,
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Re-send the ticket email for one of the caller's sales.

    Scoped to the event's organizer (or a System Administrator). The email
    covers the buyer's whole order, not just this one ticket.
    """
    service = _get_ticketing_service(db)
    return await service.resend_ticket_email_for_sale(
        ticket_id=ticket_id,
        actor=current_user,
        override_email=data.email,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.get("/{ticket_id}/download")
async def download_ticket(
    ticket_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    from fastapi import HTTPException
    from io import BytesIO

    ticket_repo = TicketRepository(Ticket, db)
    ticket = await ticket_repo.get_by_id(ticket_id, id_field="ticket_id")
    if not ticket or not is_owned_by(ticket, current_user):
        raise HTTPException(status_code=404, detail="Ticket not found")

    # Generate QR code
    qr_bytes = generate_qr_code(ticket.qr_code_data)

    # Get event and ticket type info
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(ticket.event_id, id_field="event_id")
    banner_bytes = await _fetch_image_bytes(event.banner_image_url if event else None)
    org_logo_bytes = await _fetch_organizer_logo_bytes(db, event.created_by if event else None)

    pdf_bytes = generate_ticket_pdf(
        event_title=event.title if event else "Event",
        event_date=event.event_date if event else date.today(),
        event_time=event.event_time if event else time(0, 0),
        location=event.location if event else "",
        ticket_type=ticket.ticket_type.type_name if ticket.ticket_type else "",
        ticket_code=ticket.ticket_code,
        attendee_name=current_user.full_name,
        purchase_date=ticket.purchase_date,
        qr_bytes=qr_bytes,
        banner_image_bytes=banner_bytes,
        org_logo_bytes=org_logo_bytes,
    )

    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="ticket-{ticket.ticket_code}.pdf"'
        },
    )


@router.post("/validate", response_model=TicketValidationResponse)
async def validate_ticket(
    data: TicketValidation,
    request: Request,
    current_user: User = Depends(
        require_roles("System Administrator", "Event Organizer")
    ),
    db: AsyncSession = Depends(get_db),
):
    service = _get_ticketing_service(db)
    return await service.validate_ticket(
        ticket_code=data.ticket_code,
        scanned_by=current_user.user_id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.post("/{ticket_id}/cancel", response_model=TicketResponse)
async def cancel_ticket(
    ticket_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    service = _get_ticketing_service(db)
    return await service.cancel_ticket(ticket_id, current_user)
