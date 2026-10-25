from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ownership import require_election_ownership, require_event_ownership, require_template_ownership
from app.models.election import Election
from app.models.event import Event
from app.models.template import Template
from app.models.user import User
from app.repositories.election_repository import ElectionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.template_repository import TemplateRepository
from app.schemas.template import TemplateCreate, TemplateResponse


class TemplateService:
    """Save/list/delete for organizer-saved templates. Applying a template
    (pre-filling the create wizard) is entirely client-side — see
    apply-template.tsx — this only backs the save/list/delete side."""

    def __init__(
        self,
        db: AsyncSession,
        template_repo: TemplateRepository,
        org_repo: OrganizationRepository,
        election_repo: ElectionRepository,
        event_repo: EventRepository,
    ):
        self.db = db
        self.template_repo = template_repo
        self.org_repo = org_repo
        self.election_repo = election_repo
        self.event_repo = event_repo

    async def create(self, user: User, data: TemplateCreate) -> TemplateResponse:
        if data.kind == "election":
            source = await self.election_repo.get_with_categories(data.source_id)
            if not source:
                raise HTTPException(status_code=404, detail="Election not found")
            await require_election_ownership(source, user, self.db)
            categories = [c.name for c in source.categories]
            ticket_tiers = None
        else:
            # Event.ticket_types and Event.categories are both lazy="selectin"
            # at the model level, so either get_with_* method eager-loads both.
            source = await self.event_repo.get_with_ticket_types(data.source_id)
            if not source:
                raise HTTPException(status_code=404, detail="Event not found")
            await require_event_ownership(source, user, self.db)
            categories = [c.name for c in source.categories]
            ticket_tiers = [
                {
                    "name": t.type_name,
                    "price": str(t.price),
                    "description": t.description or "",
                    "available": t.quantity_available,
                }
                for t in source.ticket_types
            ] or None

        voting_type = None
        vote_price_pesewas = None
        if categories:
            voting_type = "paid" if source.vote_price else "free"
            vote_price_pesewas = source.vote_price

        org_id = await self.org_repo.get_primary_org_id_for_user(user.user_id)
        template = await self.template_repo.create(
            {
                "org_id": org_id,
                "created_by": user.user_id,
                "kind": data.kind,
                "title": data.title,
                "description": data.description,
                "visibility": data.visibility,
                "categories": categories or None,
                "ticket_tiers": ticket_tiers,
                "voting_type": voting_type,
                "vote_price_pesewas": vote_price_pesewas,
            }
        )
        return self._to_response(template, user, org_id)

    async def list_for_user(self, user: User) -> List[TemplateResponse]:
        org_id = await self.org_repo.get_primary_org_id_for_user(user.user_id)
        templates = await self.template_repo.get_visible_to(user.user_id, org_id)
        return [self._to_response(t, user, org_id) for t in templates]

    async def delete(self, template_id: UUID, user: User) -> None:
        template = await self.template_repo.get_by_id(template_id, id_field="template_id")
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        await require_template_ownership(template, user, self.db)
        await self.template_repo.delete(template_id, id_field="template_id")

    @staticmethod
    def _to_response(template: Template, user: User, org_id: Optional[UUID]) -> TemplateResponse:
        # Cheap equivalent of the ownership check (no per-row async call):
        # same "org teammate or the creator" shape as _owns_resource, using
        # the org_id already resolved once for the whole list.
        is_mine = template.created_by == user.user_id or (
            template.org_id is not None and template.org_id == org_id
        )
        return TemplateResponse(
            template_id=template.template_id,
            kind=template.kind,
            title=template.title,
            description=template.description,
            visibility=template.visibility,
            categories=template.categories or [],
            ticket_tiers=template.ticket_tiers or [],
            voting_type=template.voting_type,
            vote_price_pesewas=template.vote_price_pesewas,
            is_mine=is_mine,
            created_at=template.created_at,
        )
