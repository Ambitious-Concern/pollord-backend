"""Read side of the org-scoped team activity feed — see
OrgActivityLogRepository for the write side (log_activity), called from
wherever a notable org action happens across the other endpoint files.
Kept separate from organizations.py, which is already large."""
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_active_user
from app.db.tenant import get_tenant_db
from app.models.org_activity_log import OrgActivityLog
from app.models.organization import Organization, OrganizationMember
from app.models.user import User
from app.repositories.org_activity_log_repository import OrgActivityLogRepository
from app.repositories.organization_repository import OrganizationRepository
from app.schemas.org_activity_log import ActivityActor, OrgActivityLogResponse

router = APIRouter(prefix="/organizations", tags=["Organization Activity"])


@router.get("/{org_id}/activity", response_model=List[OrgActivityLogResponse])
async def list_org_activity(
    org_id: UUID,
    category: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_tenant_db),
):
    """Any team member (owner/admin/editor/member) can view their org's
    activity feed — unlike most organization-management actions, this is
    read-only so it isn't restricted to owner/admin."""
    org_repo = OrganizationRepository(Organization, db)
    if not await org_repo.get_member(org_id, current_user.user_id):
        raise HTTPException(status_code=403, detail="You are not a member of this organization")

    logs = await OrgActivityLogRepository(OrgActivityLog, db).get_by_org(
        org_id, category=category, skip=skip, limit=limit
    )

    members = await db.execute(
        select(OrganizationMember).where(OrganizationMember.org_id == org_id)
    )
    roles_by_user = {m.user_id: m.role for m in members.scalars().all()}

    return [
        OrgActivityLogResponse(
            log_id=entry.log_id,
            actor=ActivityActor(
                user_id=entry.user_id,
                name=entry.user.full_name if entry.user else "Pollord System",
                role=roles_by_user.get(entry.user_id) if entry.user_id else None,
                is_system=entry.user_id is None,
            ),
            category=entry.category,
            verb=entry.verb,
            target_name=entry.target_name,
            detail=entry.detail,
            created_at=entry.created_at,
            ip_address=entry.ip_address,
            user_agent=entry.user_agent,
        )
        for entry in logs
    ]
