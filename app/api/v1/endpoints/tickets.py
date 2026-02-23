from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_active_user, require_roles
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.event import Event, TicketType
from app.models.ticket import Ticket, TicketPurchase
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.event_repository import EventRepository, TicketTypeRepository
from app.repositories.ticket_repository import TicketPurchaseRepository, TicketRepository
from app.schemas.ticket import (
    TicketPurchaseRequest,
    TicketPurchaseResponse,
    TicketResponse,
    TicketValidation,
    TicketValidationResponse,
)
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
