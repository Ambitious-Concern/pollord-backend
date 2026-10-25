"""Support ticketing: organizer-facing create/reply, plus platform-admin
triage. Priority tickets (Premium+ orgs) sort first in the admin queue —
see SupportService.create_ticket."""
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import CONSOLE_READ_ROLES, get_current_active_user, require_roles
from app.db.base import get_db
from app.models.user import User
from app.schemas.support import (
    SupportMessageCreate,
    SupportTicketCreate,
    SupportTicketResponse,
    UpdateTicketStatus,
)
from app.services.support_service import SupportService

router = APIRouter(prefix="/support", tags=["Support"])

ADMIN_ROLE = "System Administrator"


@router.post("/tickets", response_model=SupportTicketResponse, status_code=201)
async def create_ticket(
    payload: SupportTicketCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    service = SupportService(db)
    return await service.create_ticket(
        current_user.user_id, payload.category, payload.subject, payload.message
    )


@router.get("/tickets", response_model=List[SupportTicketResponse])
async def list_my_tickets(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    service = SupportService(db)
    return await service.list_my_tickets(current_user.user_id)


@router.get("/tickets/{ticket_id}", response_model=SupportTicketResponse)
async def get_my_ticket(
    ticket_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    service = SupportService(db)
    return await service.get_my_ticket(ticket_id, current_user.user_id)


@router.post("/tickets/{ticket_id}/messages", response_model=SupportTicketResponse)
async def reply_to_ticket(
    ticket_id: UUID,
    payload: SupportMessageCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    service = SupportService(db)
    return await service.reply_as_customer(ticket_id, current_user.user_id, payload.text)


# ── Platform-admin triage ────────────────────────────────────────────────


@router.get("/admin/tickets", response_model=List[SupportTicketResponse])
async def admin_list_tickets(
    status: Optional[str] = None,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    service = SupportService(db)
    return await service.list_all_tickets(status=status)


@router.get("/admin/tickets/{ticket_id}", response_model=SupportTicketResponse)
async def admin_get_ticket(
    ticket_id: UUID,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    service = SupportService(db)
    return await service.get_ticket(ticket_id)


@router.post("/admin/tickets/{ticket_id}/messages", response_model=SupportTicketResponse)
async def admin_reply_to_ticket(
    ticket_id: UUID,
    payload: SupportMessageCreate,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    service = SupportService(db)
    return await service.reply_as_agent(ticket_id, current_user.user_id, payload.text)


@router.patch("/admin/tickets/{ticket_id}/status", response_model=SupportTicketResponse)
async def admin_update_ticket_status(
    ticket_id: UUID,
    payload: UpdateTicketStatus,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    service = SupportService(db)
    return await service.update_status(ticket_id, payload.status)
