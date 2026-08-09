import secrets
from decimal import Decimal
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_current_active_user, require_roles
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.event import Event, TicketType
from app.models.ticket import Ticket, TicketPurchase
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.event_repository import EventRepository, TicketTypeRepository
from app.repositories.ticket_repository import TicketPurchaseRepository, TicketRepository
from app.repositories.ticket_transaction_repository import TicketTransactionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.ticket import (
    TicketPaymentInitResponse,
    TicketPurchaseRequest,
    TicketPurchaseResponse,
    TicketResponse,
    TicketValidation,
    TicketValidationResponse,
    VerifyAndPurchaseRequest,
)
from app.services.paystack_service import PaystackService
from app.services.ticketing_service import TicketingService
from app.utils.pdf_generator import generate_ticket_pdf
from app.utils.qr_code import generate_qr_code

router = APIRouter(prefix="/tickets", tags=["Tickets"])


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
            detail="This purchase is free — use POST /tickets/purchase instead",
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
    if txn.status == "success":
        raise HTTPException(status_code=http_status.HTTP_409_CONFLICT, detail="This payment has already been fulfilled")
    if txn.status in ("failed", "needs_refund"):
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Payment failed — please try again")

    paystack = PaystackService(settings.PAYSTACK_SECRET_KEY)
    ps_data = await paystack.verify_transaction(data.reference)

    if ps_data.get("status") != "success":
        await txn_repo.update_status(data.reference, "failed", ps_data)
        raise HTTPException(status_code=http_status.HTTP_402_PAYMENT_REQUIRED, detail="Payment was not successful")

    if ps_data.get("amount", 0) < int(txn.amount * 100):
        await txn_repo.update_status(data.reference, "failed", ps_data)
        raise HTTPException(
            status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
            detail="Payment amount does not match the ticket price",
        )

    service = _get_ticketing_service(db)
    return await service.fulfill_paid_purchase(data.reference, ps_data, txn_repo)


@router.get("/my-tickets", response_model=List[TicketResponse])
async def get_my_tickets(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 20,
):
    service = _get_ticketing_service(db)
    return await service.get_user_tickets(current_user.user_id, skip, limit)


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
    if not ticket or ticket.user_id != current_user.user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")

    # Generate QR code
    qr_bytes = generate_qr_code(ticket.qr_code_data)

    # Get event and ticket type info
    event_repo = EventRepository(Event, db)
    event = await event_repo.get_by_id(ticket.event_id, id_field="event_id")

    pdf_bytes = generate_ticket_pdf(
        event_title=event.title if event else "Event",
        event_date=str(event.event_date) if event else "",
        event_time=str(event.event_time) if event else "",
        location=event.location if event else "",
        ticket_type=ticket.ticket_type.type_name if ticket.ticket_type else "",
        ticket_code=ticket.ticket_code,
        attendee_name=current_user.full_name,
        qr_bytes=qr_bytes,
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
    return await service.cancel_ticket(ticket_id, current_user.user_id)
