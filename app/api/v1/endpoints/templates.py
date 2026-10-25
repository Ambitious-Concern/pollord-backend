"""Organizer save/list/delete for reusable election/event templates. Applying
a template is entirely client-side (pre-fills the create wizard's draft) —
see TemplateService for the actual snapshot/visibility logic."""
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_roles
from app.db.tenant import get_tenant_db
from app.models.election import Election
from app.models.event import Event
from app.models.organization import Organization
from app.models.template import Template
from app.models.user import User
from app.repositories.election_repository import ElectionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.template_repository import TemplateRepository
from app.schemas.template import TemplateCreate, TemplateResponse
from app.services.template_service import TemplateService

router = APIRouter(prefix="/templates", tags=["Templates"])

ORGANIZER_ROLES = ("System Administrator", "Election Administrator", "Event Organizer")


def _get_service(db: AsyncSession) -> TemplateService:
    return TemplateService(
        db=db,
        template_repo=TemplateRepository(Template, db),
        org_repo=OrganizationRepository(Organization, db),
        election_repo=ElectionRepository(Election, db),
        event_repo=EventRepository(Event, db),
    )


@router.post("", response_model=TemplateResponse, status_code=201)
async def create_template(
    data: TemplateCreate,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    return await _get_service(db).create(current_user, data)


@router.get("", response_model=List[TemplateResponse])
async def list_templates(
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    return await _get_service(db).list_for_user(current_user)


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: UUID,
    current_user: User = Depends(require_roles(*ORGANIZER_ROLES)),
    db: AsyncSession = Depends(get_tenant_db),
):
    await _get_service(db).delete(template_id, current_user)
