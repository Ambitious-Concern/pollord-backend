"""Organizer CRUD for discount codes. See PromoCodeService for the actual
logic and TicketingService/voting.py for where a code is applied at
checkout — this file is just request wiring."""
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_roles
from app.core.request_meta import client_ip_and_agent
from app.db.tenant import get_tenant_db
from app.models.election import Election
from app.models.event import Event
from app.models.organization import Organization
from app.models.promo_code import PromoCode
from app.models.user import User
from app.repositories.election_repository import ElectionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.promo_code_repository import PromoCodeRepository
from app.schemas.promo_code import PromoCodeCreate, PromoCodeResponse, PromoCodeUpdate
from app.services.promo_code_service import PromoCodeService

router = APIRouter(prefix="/promo-codes", tags=["Promo Codes"])

ORGANIZER_ROLES = ("System Administrator", "Election Administrator", "Event Organizer")


def _get_service(db: AsyncSession) -> PromoCodeService:
    return PromoCodeService(
        db=db,
        promo_repo=PromoCodeRepository(PromoCode, db),
        org_repo=OrganizationRepository(Organization, db),
        election_repo=ElectionRepository(Election, db),
        event_repo=EventRepository(Event, db),
    )


@router.post("", response_model=PromoCodeResponse, status_code=201)
async def create_promo_code(
    request: Request,
    data: PromoCodeCreate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    ip, agent = client_ip_and_agent(request)
    return await _get_service(db).create(current_user, data, ip=ip, user_agent=agent)


@router.get("", response_model=List[PromoCodeResponse])
async def list_promo_codes(
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    return await _get_service(db).list_for_user(current_user)


@router.put("/{promo_code_id}", response_model=PromoCodeResponse)
async def update_promo_code(
    request: Request,
    promo_code_id: UUID,
    data: PromoCodeUpdate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    ip, agent = client_ip_and_agent(request)
    return await _get_service(db).update(promo_code_id, current_user, data, ip=ip, user_agent=agent)


@router.delete("/{promo_code_id}", status_code=204)
async def delete_promo_code(
    promo_code_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    await _get_service(db).delete(promo_code_id, current_user)
