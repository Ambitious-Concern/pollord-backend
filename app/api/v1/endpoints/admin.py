from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_roles
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.organization import Organization
from app.models.user import User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.user_repository import UserRepository
from app.schemas.organization import OrganizationResponse
from app.schemas.user import (
    AssignRoles,
    AuditLogResponse,
    RoleResponse,
    UpdateAccountStatus,
    UserResponse,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/admin", tags=["Admin"])

ADMIN_ROLE = "System Administrator"


@router.get("/users", response_model=List[UserResponse])
async def list_users(
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 20,
    search: Optional[str] = None,
):
    user_repo = UserRepository(User, db)
    if search:
        users = await user_repo.search_users(search, skip, limit)
    else:
        users = await user_repo.get_all(skip=skip, limit=limit)

    results = []
    for u in users:
        user_with_roles = await user_repo.get_with_roles(u.user_id)
        roles = (
            [ur.role.role_name for ur in user_with_roles.user_roles]
            if user_with_roles and user_with_roles.user_roles
            else []
        )
        results.append(
            UserResponse(
                user_id=u.user_id,
                email=u.email,
                full_name=u.full_name,
                phone_number=u.phone_number,
                email_verified=u.email_verified,
                account_status=u.account_status,
                created_at=u.created_at,
                roles=roles,
            )
        )
    return results


@router.put("/users/{user_id}/status", response_model=UserResponse)
async def update_user_status(
    user_id: UUID,
    data: UpdateAccountStatus,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    user_repo = UserRepository(User, db)
    user = await user_repo.update_account_status(user_id, data.status)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user_with_roles = await user_repo.get_with_roles(user_id)
    roles = [ur.role.role_name for ur in user_with_roles.user_roles] if user_with_roles.user_roles else []

    await AuditLogRepository(AuditLog, db).log_action(
        action_type="UPDATE_STATUS",
        entity_type="User",
        entity_id=user_id,
        user_id=current_user.user_id,
        changes={"status": data.status},
    )

    return UserResponse(
        user_id=user.user_id,
        email=user.email,
        full_name=user.full_name,
        phone_number=user.phone_number,
        email_verified=user.email_verified,
        account_status=user.account_status,
        created_at=user.created_at,
        roles=roles,
    )


@router.put("/users/{user_id}/roles")
async def assign_roles(
    user_id: UUID,
    data: AssignRoles,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    user_repo = UserRepository(User, db)
    user = await user_repo.get_by_id(user_id, id_field="user_id")
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Remove existing roles and assign new ones
    await user_repo.remove_user_roles(user_id)
    for role_id in data.role_ids:
        await user_repo.assign_role(
            user_id, role_id, assigned_by=current_user.user_id
        )

    await AuditLogRepository(AuditLog, db).log_action(
        action_type="ASSIGN_ROLES",
        entity_type="User",
        entity_id=user_id,
        user_id=current_user.user_id,
        changes={"role_ids": [str(r) for r in data.role_ids]},
    )

    return {"message": "Roles updated successfully"}


@router.get("/roles", response_model=List[RoleResponse])
async def list_roles(
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    user_repo = UserRepository(User, db)
    roles = await user_repo.get_all_roles()
    return [RoleResponse.model_validate(r) for r in roles]


@router.get("/audit-logs", response_model=List[AuditLogResponse])
async def list_audit_logs(
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 50,
    entity_type: Optional[str] = None,
):
    audit_repo = AuditLogRepository(AuditLog, db)
    if entity_type:
        # Filter by entity type via raw query
        from sqlalchemy import select
        result = await db.execute(
            select(AuditLog)
            .where(AuditLog.entity_type == entity_type)
            .order_by(AuditLog.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        logs = list(result.scalars().all())
    else:
        logs = await audit_repo.get_recent(skip, limit)

    return [AuditLogResponse.model_validate(log) for log in logs]


# --- Organization management ---

class VerifyOrganizationRequest(BaseModel):
    is_verified: bool


def _org_to_response(org: Organization) -> OrganizationResponse:
    from app.schemas.organization import OrganizationMemberResponse
    members = []
    for m in (org.members or []):
        members.append(
            OrganizationMemberResponse(
                member_id=m.member_id,
                org_id=m.org_id,
                user_id=m.user_id,
                role=m.role,
                joined_at=m.joined_at,
                user_name=m.user.full_name if m.user else None,
                user_email=m.user.email if m.user else None,
            )
        )
    return OrganizationResponse(
        org_id=org.org_id,
        name=org.name,
        description=org.description,
        logo_url=org.logo_url,
        website=org.website,
        address=org.address,
        phone=org.phone,
        email=org.email,
        industry=org.industry,
        is_verified=org.is_verified,
        kyc_document_front=org.kyc_document_front,
        kyc_document_back=org.kyc_document_back,
        owner_id=org.owner_id,
        created_at=org.created_at,
        updated_at=org.updated_at,
        members=members,
    )


@router.get("/organizations", response_model=List[OrganizationResponse])
async def list_organizations(
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 50,
    verified: Optional[bool] = None,
):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models.organization import OrganizationMember

    query = (
        select(Organization)
        .options(selectinload(Organization.members).selectinload(OrganizationMember.user))
        .order_by(Organization.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    if verified is not None:
        query = query.where(Organization.is_verified == verified)

    result = await db.execute(query)
    orgs = list(result.scalars().all())
    return [_org_to_response(o) for o in orgs]


@router.put("/organizations/{org_id}/verify", response_model=OrganizationResponse)
async def verify_organization(
    org_id: UUID,
    data: VerifyOrganizationRequest,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    await repo.update(org_id, {"is_verified": data.is_verified}, id_field="org_id")

    await AuditLogRepository(AuditLog, db).log_action(
        action_type="VERIFY_ORGANIZATION" if data.is_verified else "REJECT_ORGANIZATION",
        entity_type="Organization",
        entity_id=org_id,
        user_id=current_user.user_id,
        changes={"is_verified": data.is_verified},
    )

    # Notify organization owner
    if org.owner and org.owner.email:
        from app.services.email_service import _base_template, send_email
        from app.core.config import settings
        status_word = "approved" if data.is_verified else "rejected"
        subject = f"Organization {status_word.capitalize()} — Pollord"
        content = f"""
        <h2>Organization {status_word.capitalize()}</h2>
        <p>Your organization <span class="highlight">{org.name}</span> has been <strong>{status_word}</strong>.</p>
        {'<p>You can now create elections and events.</p>' if data.is_verified else '<p>Please contact support for more information.</p>'}
        <p style="text-align:center; margin-top:24px;">
          <a href="{settings.FRONTEND_URL}/dashboard" class="btn">Go to Dashboard</a>
        </p>
        """
        html = _base_template(content, f"Your organization has been {status_word}")
        send_email(org.owner.email, subject, html)

    org = await repo.get_with_members(org_id)
    return _org_to_response(org)
