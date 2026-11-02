from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import CONSOLE_READ_ROLES, get_current_session_id, require_roles
from app.core.security import hash_password
from app.utils.report_document import ReportDocument, ReportSection, INTEGER
from app.utils.report_excel_generator import build_excel_bytes
from app.core.pricing import validate_vote_price
from app.core.session_revocation import revoke_session as revoke_session_in_redis
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.notification import Notification
from app.models.platform_setting import PlatformSetting
from app.models.organization import Organization
from app.models.refresh_token import RefreshToken
from app.models.user import Role, User
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository
from app.schemas.organization import OrganizationResponse
from app.schemas.user import (
    AdminSessionResponse,
    AssignRoles,
    AuditLogResponse,
    RoleResponse,
    UpdateAccountStatus,
    UserResponse,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/admin", tags=["Admin"])

ADMIN_ROLE = "System Administrator"


def _xl_dt(dt: Optional[datetime]) -> Optional[datetime]:
    """Strip timezone so openpyxl can write datetimes to Excel cells."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


@router.get("/users", response_model=List[UserResponse])
async def list_users(
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 20,
    search: Optional[str] = None,
):
    """Every user on the platform, optionally filtered by name/email search."""
    user_repo = UserRepository(User, db)
    if search:
        users = await user_repo.search_users(search, skip, limit)
    else:
        # Newest first, so someone just added is on the first page rather
        # than wherever an unordered scan happens to put them.
        users = await user_repo.get_all(
            skip=skip, limit=limit, order_by=User.created_at.desc()
        )

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


class AdminCreateUser(BaseModel):
    full_name: str
    email: str
    password: str
    phone_number: Optional[str] = None
    role_ids: List[UUID] = []
    email_verified: bool = True
    account_status: str = "active"


@router.post("/users", response_model=UserResponse, status_code=201)
async def admin_create_user(
    data: AdminCreateUser,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    """Create a user account with admin overrides: pre-verified email, role assignment,
    and status control — bypasses the normal OTP registration flow."""
    user_repo = UserRepository(User, db)
    if await user_repo.get_by_email(data.email):
        raise HTTPException(status_code=409, detail="Email already registered")

    user = await user_repo.create({
        "email": data.email.strip().lower(),
        "password_hash": hash_password(data.password),
        "full_name": data.full_name,
        "phone_number": data.phone_number,
        "email_verified": data.email_verified,
        "account_status": data.account_status,
    })

    # Default roles (Voter + Event Attendee) then any extra role_ids requested
    await user_repo.grant_roles_by_name(user.user_id, ["Voter", "Event Attendee"])
    for role_id in data.role_ids:
        await user_repo.assign_role(user.user_id, role_id, assigned_by=current_user.user_id)

    await AuditLogRepository(AuditLog, db).log_action(
        action_type="ADMIN_CREATE_USER",
        entity_type="User",
        entity_id=user.user_id,
        user_id=current_user.user_id,
        changes={"email": data.email, "email_verified": data.email_verified, "account_status": data.account_status},
    )

    user_with_roles = await user_repo.get_with_roles(user.user_id)
    roles = [ur.role.role_name for ur in user_with_roles.user_roles] if user_with_roles.user_roles else []
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


@router.get("/users/export")
async def export_users_excel(
    search: Optional[str] = None,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Download an Excel export of all platform users."""
    import io
    import openpyxl

    user_repo = UserRepository(User, db)
    if search:
        users = await user_repo.search_users(search, skip=0, limit=10000)
    else:
        users = await user_repo.get_all(skip=0, limit=10000, order_by=User.created_at.desc())

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Users"
    ws.append(["Full Name", "Email", "Phone", "Roles", "Status", "Email Verified", "Joined Date"])
    for u in users:
        user_with_roles = await user_repo.get_with_roles(u.user_id)
        roles = (
            ", ".join(ur.role.role_name for ur in user_with_roles.user_roles)
            if user_with_roles and user_with_roles.user_roles
            else ""
        )
        ws.append([
            u.full_name,
            u.email,
            u.phone_number or "",
            roles,
            u.account_status,
            "Yes" if u.email_verified else "No",
            _xl_dt(u.created_at),
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"users_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.put("/users/{user_id}/status", response_model=UserResponse)
async def update_user_status(
    user_id: UUID,
    data: UpdateAccountStatus,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    """Suspend/reactivate/deactivate an account."""
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
    """Replace a user's roles wholesale with the given role_ids."""
    user_repo = UserRepository(User, db)
    user = await user_repo.get_by_id(user_id, id_field="user_id")
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

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


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: UUID,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a user by deactivating their account.

    Hard deletion is intentionally avoided — the user's elections, votes,
    transactions, and tickets are financial/audit records that must remain
    intact. Deactivation prevents login while keeping all linked data.
    """
    user_repo = UserRepository(User, db)
    user = await user_repo.get_by_id(user_id, id_field="user_id")
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user_id == current_user.user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    await user_repo.update_account_status(user_id, "deactivated")
    await AuditLogRepository(AuditLog, db).log_action(
        action_type="DELETE_USER",
        entity_type="User",
        entity_id=user_id,
        user_id=current_user.user_id,
    )


class UserContentItem(BaseModel):
    id: str
    title: str
    kind: str          # "election" | "event"
    status: str
    created_at: str


class UserActivityItem(BaseModel):
    log_id: str
    action_type: str
    entity_type: str
    entity_id: Optional[str]
    ip_address: Optional[str]
    created_at: str


@router.get("/users/{user_id}/elections", response_model=List[UserContentItem])
async def list_user_elections(
    user_id: UUID,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
    limit: int = 20,
):
    """Elections and events created by this user."""
    from sqlalchemy import select, union_all, literal
    from app.models.election import Election
    from app.models.event import Event

    elections_result = await db.execute(
        select(Election)
        .where(Election.created_by == user_id)
        .order_by(Election.created_at.desc())
        .limit(limit)
    )
    elections = elections_result.scalars().all()

    events_result = await db.execute(
        select(Event)
        .where(Event.created_by == user_id)
        .order_by(Event.created_at.desc())
        .limit(limit)
    )
    events = events_result.scalars().all()

    items = [
        UserContentItem(
            id=str(e.election_id),
            title=e.title,
            kind="election",
            status=e.status,
            created_at=e.created_at.isoformat(),
        )
        for e in elections
    ] + [
        UserContentItem(
            id=str(e.event_id),
            title=e.title,
            kind="event",
            status=e.status,
            created_at=e.created_at.isoformat(),
        )
        for e in events
    ]
    items.sort(key=lambda x: x.created_at, reverse=True)
    return items[:limit]


@router.get("/users/{user_id}/activity", response_model=List[UserActivityItem])
async def list_user_activity(
    user_id: UUID,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
    limit: int = 15,
):
    """Recent audit-log entries attributed to this user."""
    from sqlalchemy import select
    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.user_id == user_id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    logs = result.scalars().all()
    return [
        UserActivityItem(
            log_id=str(log.log_id),
            action_type=log.action_type,
            entity_type=log.entity_type,
            entity_id=str(log.entity_id) if log.entity_id else None,
            ip_address=log.ip_address,
            created_at=log.created_at.isoformat(),
        )
        for log in logs
    ]


@router.get("/roles", response_model=List[RoleResponse])
async def list_roles(
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Every role defined on the platform, for the role-assignment UI."""
    user_repo = UserRepository(User, db)
    roles = await user_repo.get_all_roles()
    return [RoleResponse.model_validate(r) for r in roles]


class TogglePermissionRequest(BaseModel):
    resource: str
    action: str
    granted: bool


@router.patch("/roles/{role_id}/permissions", response_model=RoleResponse)
async def toggle_role_permission(
    role_id: UUID,
    body: TogglePermissionRequest,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    """Toggle a single resource+action permission on a role."""
    from sqlalchemy import select as sa_select
    result = await db.execute(sa_select(Role).where(Role.role_id == role_id))
    role = result.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    perms = dict(role.permissions or {})
    actions = set(perms.get(body.resource, []))
    if body.granted:
        actions.add(body.action)
    else:
        actions.discard(body.action)
    if actions:
        perms[body.resource] = sorted(actions)
    else:
        perms.pop(body.resource, None)
    role.permissions = perms
    await db.commit()
    await db.refresh(role)
    return RoleResponse.model_validate(role)


@router.get("/audit-logs", response_model=List[AuditLogResponse])
async def list_audit_logs(
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 50,
    entity_type: Optional[str] = None,
):
    """Platform-wide audit trail, optionally filtered by entity type."""
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload

    q = (
        select(AuditLog)
        .options(joinedload(AuditLog.user))
        .order_by(AuditLog.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)

    result = await db.execute(q)
    logs = list(result.scalars().unique().all())

    return [
        AuditLogResponse(
            log_id=log.log_id,
            user_id=log.user_id,
            user_name=log.user.full_name if log.user else None,
            action_type=log.action_type,
            entity_type=log.entity_type,
            entity_id=log.entity_id,
            changes=log.changes,
            ip_address=log.ip_address,
            user_agent=log.user_agent,
            created_at=log.created_at,
        )
        for log in logs
    ]


# --- Organization management ---

class VerifyOrganizationRequest(BaseModel):
    is_verified: bool
    rejection_reason: Optional[str] = None


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
        rejection_reason=org.rejection_reason,
        kyc_document_front=org.kyc_document_front,
        kyc_document_back=org.kyc_document_back,
        owner_id=org.owner_id,
        is_active=org.is_active,
        created_at=org.created_at,
        updated_at=org.updated_at,
        members=members,
    )


@router.get("/organizations", response_model=List[OrganizationResponse])
async def list_organizations(
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 50,
    verified: Optional[bool] = None,
):
    """Every organization on the platform, optionally filtered to
    verified/unverified only."""
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


@router.get("/organizations/export")
async def export_organizations_excel(
    verified: Optional[bool] = None,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Download an Excel export of all organizations."""
    import io
    import openpyxl
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models.organization import OrganizationMember as OrgMember

    query = (
        select(Organization)
        .options(selectinload(Organization.members).selectinload(OrgMember.user))
        .order_by(Organization.created_at.desc())
        .limit(10000)
    )
    if verified is not None:
        query = query.where(Organization.is_verified == verified)

    result = await db.execute(query)
    orgs = list(result.scalars().all())

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Organizations"
    ws.append(["Name", "Industry", "Email", "Phone", "Website", "Owner", "Owner Email", "Members", "Status", "Joined Date"])
    for org in orgs:
        owner = next((m for m in (org.members or []) if m.role == 'owner'), None)
        ws.append([
            org.name,
            org.industry or "",
            org.email or "",
            org.phone or "",
            org.website or "",
            owner.user.full_name if owner and owner.user else "",
            owner.user.email if owner and owner.user else "",
            len(org.members or []),
            "Verified" if org.is_verified else "Pending",
            _xl_dt(org.created_at),
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"organizations_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class AdminCreateOrganization(BaseModel):
    name: str
    owner_user_id: UUID
    description: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    address: Optional[str] = None
    industry: Optional[str] = None
    is_verified: bool = True


@router.post("/organizations", response_model=OrganizationResponse, status_code=201)
async def admin_create_organization(
    data: AdminCreateOrganization,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    """Create an organization on behalf of a user, optionally pre-verified."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models.organization import OrganizationMember as OrgMember

    # Verify the owner user exists
    user_repo = UserRepository(User, db)
    owner = await user_repo.get(data.owner_user_id)
    if not owner:
        raise HTTPException(status_code=404, detail="Owner user not found")

    org_repo = OrganizationRepository(Organization, db)
    org = await org_repo.create({
        "name": data.name,
        "description": data.description,
        "email": data.email,
        "phone": data.phone,
        "website": data.website,
        "address": data.address,
        "industry": data.industry,
        "owner_id": data.owner_user_id,
        "is_verified": data.is_verified,
    })

    await org_repo.add_member(org_id=org.org_id, user_id=data.owner_user_id, role="owner")

    # Grant system roles so the owner can create elections and events
    ORG_MEMBER_SYSTEM_ROLES = ["Election Administrator", "Event Organizer"]
    await user_repo.grant_roles_by_name(data.owner_user_id, ORG_MEMBER_SYSTEM_ROLES)

    await AuditLogRepository(AuditLog, db).log_action(
        action_type="ADMIN_CREATE_ORGANIZATION",
        entity_type="Organization",
        entity_id=org.org_id,
        user_id=current_user.user_id,
        changes={"name": data.name, "owner_user_id": str(data.owner_user_id), "is_verified": data.is_verified},
    )

    # Reload with members
    result = await db.execute(
        select(Organization)
        .options(selectinload(Organization.members).selectinload(OrgMember.user))
        .where(Organization.org_id == org.org_id)
    )
    org = result.scalar_one()
    return _org_to_response(org)


@router.put("/organizations/{org_id}/verify", response_model=OrganizationResponse)
async def verify_organization(
    org_id: UUID,
    data: VerifyOrganizationRequest,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    """Approve or reject an organization's KYC, emailing the owner either way."""
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    update_data: dict = {"is_verified": data.is_verified}
    if not data.is_verified:
        update_data["rejection_reason"] = data.rejection_reason or None
    else:
        update_data["rejection_reason"] = None
    await repo.update(org_id, update_data, id_field="org_id")

    await AuditLogRepository(AuditLog, db).log_action(
        action_type="VERIFY_ORGANIZATION" if data.is_verified else "REJECT_ORGANIZATION",
        entity_type="Organization",
        entity_id=org_id,
        user_id=current_user.user_id,
        changes={"is_verified": data.is_verified},
    )

    if data.is_verified:
        member_ids = await repo.get_member_ids(org_id)
        await NotificationRepository(Notification, db).notify_org(
            org_id=org_id,
            member_ids=member_ids,
            type="verification",
            title="Organization verified",
            description="Your KYC documents were approved.",
            link="/dashboard/settings?section=verification",
        )

    if org.owner and org.owner.email:
        from app.services.email_service import _base_template
        from app.tasks.email_tasks import send_email_task
        status_word = "approved" if data.is_verified else "rejected"
        subject = f"Organization {status_word.capitalize()}: Pollord"
        content = f"""
        <h2>Organization {status_word.capitalize()}</h2>
        <p>Your organization <span class="highlight">{org.name}</span> has been <strong>{status_word}</strong>.</p>
        {
            '<p>You can now create elections and events.</p>'
            if data.is_verified
            else (
                f'<p><strong>Reason:</strong> {data.rejection_reason}</p>'
                if data.rejection_reason
                else '<p>Please contact support for more information.</p>'
            )
        }
        <p style="text-align:center; margin-top:24px;">
          <a href="{settings.FRONTEND_URL}/dashboard" class="btn">Go to Dashboard</a>
        </p>
        """
        html = _base_template(content, f"Your organization has been {status_word}")
        send_email_task.delay(org.owner.email, subject, html)

    org = await repo.get_with_members(org_id)
    return _org_to_response(org)


# ── Platform Settings ────────────────────────────────────────────────────────

PLATFORM_SETTING_DEFAULTS: dict[str, str] = {
    "vote_price": "100",
    "allow_user_registration": "true",
    "require_email_verification": "false",
    "allow_organization_creation": "true",
    "allow_free_elections": "false",
    "max_candidates_per_election": "0",
    "allow_public_results": "true",
    "maintenance_mode": "false",
    "maintenance_message": "The platform is currently under maintenance. Please check back later.",
    "email_notifications_enabled": "true",
    "whatsapp_notifications_enabled": "true",
    "launch_gate_enabled": "true",
    "launch_at": "2026-08-13T00:00:00+00:00",
    "celebration_window_minutes": "10080",
}


class PlatformSettingsResponse(BaseModel):
    vote_price: int  # pesewas
    allow_user_registration: bool
    require_email_verification: bool
    allow_organization_creation: bool
    allow_free_elections: bool
    max_candidates_per_election: int  # 0 = unlimited
    allow_public_results: bool
    maintenance_mode: bool
    maintenance_message: str
    email_notifications_enabled: bool
    whatsapp_notifications_enabled: bool
    launch_gate_enabled: bool
    launch_at: datetime
    celebration_window_minutes: int


class PlatformSettingsUpdate(BaseModel):
    vote_price: Optional[int] = None
    allow_user_registration: Optional[bool] = None
    require_email_verification: Optional[bool] = None
    allow_organization_creation: Optional[bool] = None
    allow_free_elections: Optional[bool] = None
    max_candidates_per_election: Optional[int] = None
    allow_public_results: Optional[bool] = None
    maintenance_mode: Optional[bool] = None
    maintenance_message: Optional[str] = None
    email_notifications_enabled: Optional[bool] = None
    whatsapp_notifications_enabled: Optional[bool] = None
    launch_gate_enabled: Optional[bool] = None
    launch_at: Optional[datetime] = None
    celebration_window_minutes: Optional[int] = None

    @field_validator("vote_price")
    @classmethod
    def validate_vote_price(cls, v: Optional[int]) -> Optional[int]:
        return validate_vote_price(v)

    @field_validator("max_candidates_per_election")
    @classmethod
    def validate_max_candidates(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 0:
            raise ValueError("Max candidates must be 0 (unlimited) or a positive integer")
        return v

    @field_validator("celebration_window_minutes")
    @classmethod
    def validate_celebration_window(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 0:
            raise ValueError("Celebration window must be 0 or a positive integer")
        return v


async def _get_platform_setting(db: AsyncSession, key: str) -> str:
    """Read one setting, falling back to PLATFORM_SETTING_DEFAULTS."""
    from sqlalchemy import select
    result = await db.execute(select(PlatformSetting).where(PlatformSetting.key == key))
    row = result.scalar_one_or_none()
    return row.value if row else PLATFORM_SETTING_DEFAULTS.get(key, "")


async def _set_platform_setting(db: AsyncSession, key: str, value: str, user_id) -> None:
    """Upsert one setting row."""
    from sqlalchemy import select
    from datetime import datetime, timezone
    result = await db.execute(select(PlatformSetting).where(PlatformSetting.key == key))
    row = result.scalar_one_or_none()
    if row:
        row.value = value
        row.updated_by = user_id
        row.updated_at = datetime.now(timezone.utc)
    else:
        db.add(PlatformSetting(key=key, value=value, updated_by=user_id))
    await db.commit()


def _to_bool(value: str) -> bool:
    return value.lower() == "true"


async def _fetch_all_settings(db: AsyncSession) -> PlatformSettingsResponse:
    """Assemble the full settings response, one _get_platform_setting call
    per field (defaults fill in anything never written to the DB)."""
    return PlatformSettingsResponse(
        vote_price=int(await _get_platform_setting(db, "vote_price")),
        allow_user_registration=_to_bool(await _get_platform_setting(db, "allow_user_registration")),
        require_email_verification=_to_bool(await _get_platform_setting(db, "require_email_verification")),
        allow_organization_creation=_to_bool(await _get_platform_setting(db, "allow_organization_creation")),
        allow_free_elections=_to_bool(await _get_platform_setting(db, "allow_free_elections")),
        max_candidates_per_election=int(await _get_platform_setting(db, "max_candidates_per_election")),
        allow_public_results=_to_bool(await _get_platform_setting(db, "allow_public_results")),
        maintenance_mode=_to_bool(await _get_platform_setting(db, "maintenance_mode")),
        maintenance_message=await _get_platform_setting(db, "maintenance_message"),
        email_notifications_enabled=_to_bool(await _get_platform_setting(db, "email_notifications_enabled")),
        whatsapp_notifications_enabled=_to_bool(await _get_platform_setting(db, "whatsapp_notifications_enabled")),
        launch_gate_enabled=_to_bool(await _get_platform_setting(db, "launch_gate_enabled")),
        launch_at=datetime.fromisoformat(await _get_platform_setting(db, "launch_at")),
        celebration_window_minutes=int(await _get_platform_setting(db, "celebration_window_minutes")),
    )


@router.get("/platform-settings", response_model=PlatformSettingsResponse)
async def get_platform_settings(
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Current platform-wide settings (vote price, feature flags, etc.)."""
    return await _fetch_all_settings(db)


@router.put("/platform-settings", response_model=PlatformSettingsResponse)
async def update_platform_settings(
    data: PlatformSettingsUpdate,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    """Partial update of platform settings — only fields present in the
    request body are changed."""
    updates: dict[str, str] = {}
    if data.vote_price is not None:
        updates["vote_price"] = str(data.vote_price)
    if data.allow_user_registration is not None:
        updates["allow_user_registration"] = str(data.allow_user_registration).lower()
    if data.require_email_verification is not None:
        updates["require_email_verification"] = str(data.require_email_verification).lower()
    if data.allow_organization_creation is not None:
        updates["allow_organization_creation"] = str(data.allow_organization_creation).lower()
    if data.allow_free_elections is not None:
        updates["allow_free_elections"] = str(data.allow_free_elections).lower()
    if data.max_candidates_per_election is not None:
        updates["max_candidates_per_election"] = str(data.max_candidates_per_election)
    if data.allow_public_results is not None:
        updates["allow_public_results"] = str(data.allow_public_results).lower()
    if data.maintenance_mode is not None:
        updates["maintenance_mode"] = str(data.maintenance_mode).lower()
    if data.maintenance_message is not None:
        updates["maintenance_message"] = data.maintenance_message
    if data.email_notifications_enabled is not None:
        updates["email_notifications_enabled"] = str(data.email_notifications_enabled).lower()
    if data.whatsapp_notifications_enabled is not None:
        updates["whatsapp_notifications_enabled"] = str(data.whatsapp_notifications_enabled).lower()
    if data.launch_gate_enabled is not None:
        updates["launch_gate_enabled"] = str(data.launch_gate_enabled).lower()
    if data.launch_at is not None:
        dt = data.launch_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        updates["launch_at"] = dt.astimezone(timezone.utc).isoformat()

    if data.celebration_window_minutes is not None:
        updates["celebration_window_minutes"] = str(data.celebration_window_minutes)

    for key, value in updates.items():
        await _set_platform_setting(db, key, value, current_user.user_id)

    if updates:
        await AuditLogRepository(AuditLog, db).log_action(
            action_type="UPDATE_PLATFORM_SETTINGS",
            entity_type="PlatformSetting",
            entity_id=None,
            user_id=current_user.user_id,
            changes=updates,
        )

    return await _fetch_all_settings(db)


# ── Election Vote Price Override ─────────────────────────────────────────────

class ElectionVotePriceOverride(BaseModel):
    vote_price: Optional[int] = None  # None = reset to global default

    @field_validator("vote_price")
    @classmethod
    def validate_price(cls, v: Optional[int]) -> Optional[int]:
        return validate_vote_price(v)


class ElectionVotePriceResponse(BaseModel):
    election_id: UUID
    vote_price: Optional[int]
    effective_vote_price: int


@router.put("/elections/{election_id}/vote-price", response_model=ElectionVotePriceResponse)
async def override_election_vote_price(
    election_id: UUID,
    data: ElectionVotePriceOverride,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    """Set (or clear, with vote_price=None) a per-election vote price
    override, distinct from the global default."""
    from sqlalchemy import select
    from app.models.election import Election

    result = await db.execute(select(Election).where(Election.election_id == election_id))
    election = result.scalar_one_or_none()
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")

    old_price = election.vote_price
    election.vote_price = data.vote_price
    await db.commit()
    await db.refresh(election)

    global_price = int(await _get_platform_setting(db, "vote_price"))
    effective = election.vote_price if election.vote_price is not None else global_price

    await AuditLogRepository(AuditLog, db).log_action(
        action_type="OVERRIDE_ELECTION_VOTE_PRICE",
        entity_type="Election",
        entity_id=election_id,
        user_id=current_user.user_id,
        changes={"vote_price": {"old": old_price, "new": data.vote_price}},
    )

    return ElectionVotePriceResponse(
        election_id=election.election_id,
        vote_price=election.vote_price,
        effective_vote_price=effective,
    )


# ── Organization Analytics ────────────────────────────────────────────────────

class ElectionAnalytics(BaseModel):
    election_id: UUID
    title: str
    status: str
    start_datetime: datetime
    end_datetime: datetime
    vote_price: Optional[int]
    effective_vote_price: int
    total_votes: int
    transaction_count: int
    total_revenue_pesewas: int


class EventAnalytics(BaseModel):
    event_id: UUID
    title: str
    status: str
    event_date: date
    total_tickets_sold: int
    total_revenue_ghs: float


class OrgMemberSummary(BaseModel):
    """Who is in the organization — the console only had a count before."""

    user_id: UUID
    user_name: Optional[str]
    user_email: Optional[str]
    role: str
    joined_at: datetime


class OrgAnalyticsResponse(BaseModel):
    org_id: UUID
    name: str
    total_elections: int
    total_events: int
    total_votes: int
    total_transactions: int
    total_members: int
    total_election_revenue_pesewas: int
    total_event_revenue_ghs: float
    # Owner first, then by join date — an admin looking for someone they just
    # added wants the newest names, and the owner is the useful anchor.
    members: List[OrgMemberSummary]
    elections: List[ElectionAnalytics]
    events: List[EventAnalytics]


@router.get("/organizations/{org_id}/analytics", response_model=OrgAnalyticsResponse)
async def get_organization_analytics(
    org_id: UUID,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Admin-only deep-dive into one organization's elections, events,
    votes, and revenue."""
    from sqlalchemy import select, func as sqlfunc
    from sqlalchemy.orm import selectinload
    from app.models.organization import Organization, OrganizationMember
    from app.models.election import Election
    from app.models.vote import Vote
    from app.models.transaction import Transaction
    from app.models.event import Event
    from app.models.ticket import TicketPurchase

    org = await db.get(Organization, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Collect the org's members, with enough identity for the console to show
    # who they are rather than just how many there are.
    member_result = await db.execute(
        select(OrganizationMember, User)
        .join(User, User.user_id == OrganizationMember.user_id)
        .where(OrganizationMember.org_id == org_id)
        .order_by(
            (OrganizationMember.role != "owner"),
            OrganizationMember.joined_at.desc(),
        )
    )
    member_rows = member_result.all()
    user_ids = [m.user_id for m, _ in member_rows]
    members = [
        OrgMemberSummary(
            user_id=m.user_id,
            user_name=u.full_name,
            user_email=u.email,
            role=m.role,
            joined_at=m.joined_at,
        )
        for m, u in member_rows
    ]

    if not user_ids:
        return OrgAnalyticsResponse(
            org_id=org.org_id,
            name=org.name,
            total_elections=0,
            total_events=0,
            total_votes=0,
            total_transactions=0,
            total_members=0,
            total_election_revenue_pesewas=0,
            total_event_revenue_ghs=0.0,
            members=[],
            elections=[],
            events=[],
        )

    # Query elections created by org members
    elections_result = await db.execute(
        select(Election).where(Election.created_by.in_(user_ids))
        .order_by(Election.created_at.desc())
    )
    elections = list(elections_result.scalars().all())
    election_ids = [e.election_id for e in elections]

    # Aggregate votes per election
    votes_by_election: dict[UUID, int] = {}
    if election_ids:
        vote_rows = await db.execute(
            select(Vote.election_id, sqlfunc.sum(Vote.count))
            .where(Vote.election_id.in_(election_ids))
            .group_by(Vote.election_id)
        )
        votes_by_election = {row[0]: int(row[1]) for row in vote_rows.all()}

    # Aggregate transaction revenue and count per election (success only)
    txn_count_by_election: dict[UUID, int] = {}
    revenue_by_election: dict[UUID, int] = {}
    if election_ids:
        txn_rows = await db.execute(
            select(
                Transaction.election_id,
                sqlfunc.count(Transaction.transaction_id),
                sqlfunc.sum(Transaction.amount),
            )
            .where(
                Transaction.election_id.in_(election_ids),
                Transaction.status == "success",
            )
            .group_by(Transaction.election_id)
        )
        for row in txn_rows.all():
            txn_count_by_election[row[0]] = int(row[1])
            revenue_by_election[row[0]] = int(row[2] or 0)

    global_vote_price = int(await _get_platform_setting(db, "vote_price"))

    election_analytics = [
        ElectionAnalytics(
            election_id=e.election_id,
            title=e.title,
            status=e.status,
            start_datetime=e.start_datetime,
            end_datetime=e.end_datetime,
            vote_price=e.vote_price,
            effective_vote_price=e.vote_price if e.vote_price is not None else global_vote_price,
            total_votes=votes_by_election.get(e.election_id, 0),
            transaction_count=txn_count_by_election.get(e.election_id, 0),
            total_revenue_pesewas=revenue_by_election.get(e.election_id, 0),
        )
        for e in elections
    ]

    # Query events created by org members
    events_result = await db.execute(
        select(Event).where(Event.created_by.in_(user_ids))
        .order_by(Event.created_at.desc())
    )
    events = list(events_result.scalars().all())
    event_ids = [e.event_id for e in events]

    # Aggregate ticket sales and revenue per event (completed purchases only)
    tickets_sold_by_event: dict[UUID, int] = {}
    revenue_by_event: dict[UUID, float] = {}
    if event_ids:
        purchase_rows = await db.execute(
            select(
                TicketPurchase.event_id,
                sqlfunc.count(TicketPurchase.purchase_id),
                sqlfunc.sum(TicketPurchase.total_amount),
            )
            .where(
                TicketPurchase.event_id.in_(event_ids),
                TicketPurchase.payment_status == "completed",
            )
            .group_by(TicketPurchase.event_id)
        )
        for row in purchase_rows.all():
            tickets_sold_by_event[row[0]] = int(row[1])
            revenue_by_event[row[0]] = float(row[2] or 0)

    event_analytics = [
        EventAnalytics(
            event_id=e.event_id,
            title=e.title,
            status=e.status,
            event_date=e.event_date,
            total_tickets_sold=tickets_sold_by_event.get(e.event_id, 0),
            total_revenue_ghs=revenue_by_event.get(e.event_id, 0.0),
        )
        for e in events
    ]

    total_votes = sum(a.total_votes for a in election_analytics)
    total_transactions = sum(a.transaction_count for a in election_analytics)
    total_election_revenue = sum(a.total_revenue_pesewas for a in election_analytics)
    total_event_revenue = sum(a.total_revenue_ghs for a in event_analytics)

    return OrgAnalyticsResponse(
        org_id=org.org_id,
        name=org.name,
        total_elections=len(elections),
        total_events=len(events),
        total_votes=total_votes,
        total_transactions=total_transactions,
        total_members=len(user_ids),
        total_election_revenue_pesewas=total_election_revenue,
        total_event_revenue_ghs=total_event_revenue,
        members=members,
        elections=election_analytics,
        events=event_analytics,
    )


# ── All Elections (Platform-wide) ────────────────────────────────────────────

class AdminElectionListItem(BaseModel):
    election_id: UUID
    title: str
    slug: Optional[str]
    status: str
    plan: str
    visibility: str
    fee_payer: str
    banner_image_url: Optional[str] = None
    vote_price: Optional[int]
    effective_vote_price: int
    total_votes: int
    total_revenue_pesewas: int
    org_id: Optional[UUID]
    org_name: Optional[str]
    category_count: int
    start_datetime: datetime
    end_datetime: datetime
    created_at: datetime


class AdminElectionListResponse(BaseModel):
    items: List[AdminElectionListItem]
    total: int
    skip: int
    limit: int


class AdminElectionCategoryDetail(BaseModel):
    category_id: UUID
    name: str
    election_type: str
    candidate_count: int
    total_votes: int
    top_candidate_name: Optional[str]


class AdminElectionDetail(AdminElectionListItem):
    description: Optional[str]
    eligible_voter_count: int
    categories: List[AdminElectionCategoryDetail]


# ── Admin nominees ────────────────────────────────────────────────────────────

class AdminNomineeItem(BaseModel):
    candidate_id: UUID
    name: str
    short_code: Optional[str] = None
    image_url: Optional[str] = None
    category_id: UUID
    category_name: str
    parent_id: UUID
    parent_type: str  # "election" | "event"
    parent_title: str
    org_id: Optional[UUID] = None
    org_name: Optional[str] = None
    created_at: datetime


class AdminNomineeListResponse(BaseModel):
    items: List[AdminNomineeItem]
    total: int
    skip: int
    limit: int


@router.get("/nominees", response_model=AdminNomineeListResponse)
async def list_all_nominees(
    org_id: Optional[UUID] = None,
    parent_type: Optional[str] = None,
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Platform-wide nominee list with parent election/event and org context."""
    from sqlalchemy import select, or_
    from app.models.election import Category, Candidate, Election
    from app.models.event import Event

    items: list[AdminNomineeItem] = []

    if parent_type != "event":
        stmt = (
            select(
                Candidate.candidate_id,
                Candidate.name.label("name"),
                Candidate.short_code,
                Candidate.image_url,
                Candidate.category_id,
                Candidate.created_at,
                Category.name.label("category_name"),
                Election.election_id.label("parent_id"),
                Election.title.label("parent_title"),
                Election.org_id.label("org_id"),
                Organization.name.label("org_name"),
            )
            .join(Category, Category.category_id == Candidate.category_id)
            .join(Election, Election.election_id == Category.election_id)
            .outerjoin(Organization, Organization.org_id == Election.org_id)
            .where(Category.election_id.isnot(None))
        )
        if org_id:
            stmt = stmt.where(Election.org_id == org_id)
        if search:
            stmt = stmt.where(
                or_(Candidate.name.ilike(f"%{search}%"), Election.title.ilike(f"%{search}%"))
            )
        for r in (await db.execute(stmt)).all():
            items.append(AdminNomineeItem(
                candidate_id=r.candidate_id,
                name=r.name,
                short_code=r.short_code,
                image_url=r.image_url,
                category_id=r.category_id,
                category_name=r.category_name,
                parent_id=r.parent_id,
                parent_type="election",
                parent_title=r.parent_title,
                org_id=r.org_id,
                org_name=r.org_name,
                created_at=r.created_at,
            ))

    if parent_type != "election":
        stmt = (
            select(
                Candidate.candidate_id,
                Candidate.name.label("name"),
                Candidate.short_code,
                Candidate.image_url,
                Candidate.category_id,
                Candidate.created_at,
                Category.name.label("category_name"),
                Event.event_id.label("parent_id"),
                Event.title.label("parent_title"),
                Event.org_id.label("org_id"),
                Organization.name.label("org_name"),
            )
            .join(Category, Category.category_id == Candidate.category_id)
            .join(Event, Event.event_id == Category.event_id)
            .outerjoin(Organization, Organization.org_id == Event.org_id)
            .where(Category.event_id.isnot(None))
        )
        if org_id:
            stmt = stmt.where(Event.org_id == org_id)
        if search:
            stmt = stmt.where(
                or_(Candidate.name.ilike(f"%{search}%"), Event.title.ilike(f"%{search}%"))
            )
        for r in (await db.execute(stmt)).all():
            items.append(AdminNomineeItem(
                candidate_id=r.candidate_id,
                name=r.name,
                short_code=r.short_code,
                image_url=r.image_url,
                category_id=r.category_id,
                category_name=r.category_name,
                parent_id=r.parent_id,
                parent_type="event",
                parent_title=r.parent_title,
                org_id=r.org_id,
                org_name=r.org_name,
                created_at=r.created_at,
            ))

    items.sort(key=lambda x: x.created_at, reverse=True)
    total = len(items)
    return AdminNomineeListResponse(items=items[skip: skip + limit], total=total, skip=skip, limit=limit)


# ── Admin results ──────────────────────────────────────────────────────────────

class AdminResultCandidate(BaseModel):
    candidate_id: UUID
    name: str
    short_code: Optional[str] = None
    image_url: Optional[str] = None
    vote_count: int
    percentage: float
    rank: int


class AdminResultCategory(BaseModel):
    category_id: UUID
    name: str
    election_type: str
    total_votes: int
    candidates: List[AdminResultCandidate]


class AdminResultsResponse(BaseModel):
    parent_id: UUID
    parent_type: str  # "election" | "event"
    parent_title: str
    org_id: Optional[UUID] = None
    org_name: Optional[str] = None
    total_votes: int
    categories: List[AdminResultCategory]


@router.get("/results", response_model=AdminResultsResponse)
async def get_admin_results(
    parent_id: UUID,
    parent_type: str,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Decrypt and tally votes for a single election or event."""
    from sqlalchemy import select, or_
    from sqlalchemy.orm import selectinload
    from app.models.election import Election, Category
    from app.models.event import Event
    from app.models.organization import Organization
    from app.models.vote import Vote
    from app.services.voting_service import tally_votes_by_category
    from app.services.cryptography_service import CryptographyService

    if parent_type == "election":
        stmt = (
            select(Election, Organization.name.label("org_name"))
            .outerjoin(Organization, Organization.org_id == Election.org_id)
            .options(selectinload(Election.categories).selectinload(Category.candidates))
            .where(Election.election_id == parent_id)
        )
        row = (await db.execute(stmt)).first()
        if not row:
            raise HTTPException(status_code=404, detail="Election not found")
        parent, org_name = row.Election, row.org_name
        categories = list(parent.categories)
        votes = (
            await db.execute(select(Vote).where(Vote.election_id == parent_id))
        ).scalars().all()
        parent_title = parent.title
        parent_org_id = parent.org_id
    elif parent_type == "event":
        stmt = (
            select(Event, Organization.name.label("org_name"))
            .outerjoin(Organization, Organization.org_id == Event.org_id)
            .options(selectinload(Event.categories).selectinload(Category.candidates))
            .where(Event.event_id == parent_id)
        )
        row = (await db.execute(stmt)).first()
        if not row:
            raise HTTPException(status_code=404, detail="Event not found")
        parent, org_name = row.Event, row.org_name
        categories = list(parent.categories)
        votes = (
            await db.execute(select(Vote).where(Vote.event_id == parent_id))
        ).scalars().all()
        parent_title = parent.title
        parent_org_id = parent.org_id
    else:
        raise HTTPException(status_code=400, detail="parent_type must be 'election' or 'event'")

    category_results = tally_votes_by_category(list(votes), categories, CryptographyService())
    total_votes = sum(cr.total_votes for cr in category_results)

    result_categories = []
    for cr in category_results:
        result_categories.append(
            AdminResultCategory(
                category_id=cr.category_id,
                name=cr.name,
                election_type=cr.election_type,
                total_votes=cr.total_votes,
                candidates=[
                    AdminResultCandidate(
                        candidate_id=r.candidate_id,
                        name=r.name,
                        short_code=next(
                            (c.short_code for cat in categories for c in cat.candidates
                             if c.candidate_id == r.candidate_id),
                            None,
                        ),
                        image_url=next(
                            (c.image_url for cat in categories for c in cat.candidates
                             if c.candidate_id == r.candidate_id),
                            None,
                        ),
                        vote_count=r.vote_count,
                        percentage=r.percentage,
                        rank=i + 1,
                    )
                    for i, r in enumerate(cr.results)
                ],
            )
        )

    return AdminResultsResponse(
        parent_id=parent_id,
        parent_type=parent_type,
        parent_title=parent_title,
        org_id=parent_org_id,
        org_name=org_name,
        total_votes=total_votes,
        categories=result_categories,
    )


@router.get("/elections", response_model=AdminElectionListResponse)
async def list_all_elections(
    status: Optional[str] = None,
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Platform-wide election list with org name and revenue aggregation."""
    from sqlalchemy import select, func as sqlfunc, outerjoin
    from sqlalchemy.orm import selectinload
    from app.models.election import Election
    from app.models.organization import Organization
    from app.models.vote import Vote
    from app.models.transaction import Transaction

    stmt = (
        select(Election, Organization.name.label("org_name"))
        .outerjoin(Organization, Organization.org_id == Election.org_id)
    )
    if status:
        stmt = stmt.where(Election.status == status)
    if search:
        stmt = stmt.where(Election.title.ilike(f"%{search}%"))

    count_stmt = select(sqlfunc.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(Election.created_at.desc()).offset(skip).limit(limit)
    rows = (await db.execute(stmt)).all()

    election_ids = [r.Election.election_id for r in rows]

    votes_by_election: dict[UUID, int] = {}
    revenue_by_election: dict[UUID, int] = {}
    if election_ids:
        vote_rows = await db.execute(
            select(Vote.election_id, sqlfunc.sum(Vote.count))
            .where(Vote.election_id.in_(election_ids))
            .group_by(Vote.election_id)
        )
        votes_by_election = {r[0]: int(r[1]) for r in vote_rows.all()}

        txn_rows = await db.execute(
            select(Transaction.election_id, sqlfunc.sum(Transaction.amount))
            .where(
                Transaction.election_id.in_(election_ids),
                Transaction.status == "success",
            )
            .group_by(Transaction.election_id)
        )
        revenue_by_election = {r[0]: int(r[1] or 0) for r in txn_rows.all()}

    global_price = int(await _get_platform_setting(db, "vote_price"))

    items = [
        AdminElectionListItem(
            election_id=r.Election.election_id,
            title=r.Election.title,
            slug=r.Election.slug,
            status=r.Election.status,
            plan=r.Election.plan,
            visibility=r.Election.visibility,
            fee_payer=r.Election.fee_payer,
            banner_image_url=r.Election.banner_image_url,
            vote_price=r.Election.vote_price,
            effective_vote_price=(
                r.Election.vote_price if r.Election.vote_price is not None else global_price
            ),
            total_votes=votes_by_election.get(r.Election.election_id, 0),
            total_revenue_pesewas=revenue_by_election.get(r.Election.election_id, 0),
            org_id=r.Election.org_id,
            org_name=r.org_name,
            category_count=len(r.Election.categories),
            start_datetime=r.Election.start_datetime,
            end_datetime=r.Election.end_datetime,
            created_at=r.Election.created_at,
        )
        for r in rows
    ]

    return AdminElectionListResponse(items=items, total=total, skip=skip, limit=limit)


@router.get("/elections/export")
async def export_elections_excel(
    status: Optional[str] = None,
    search: Optional[str] = None,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Download an Excel export of all elections."""
    import io
    import openpyxl
    from sqlalchemy import select, func as sqlfunc
    from app.models.election import Election
    from app.models.organization import Organization
    from app.models.vote import Vote
    from app.models.transaction import Transaction

    stmt = (
        select(Election, Organization.name.label("org_name"))
        .outerjoin(Organization, Organization.org_id == Election.org_id)
    )
    if status:
        stmt = stmt.where(Election.status == status)
    if search:
        stmt = stmt.where(Election.title.ilike(f"%{search}%"))
    stmt = stmt.order_by(Election.created_at.desc()).limit(10000)
    rows = (await db.execute(stmt)).all()

    election_ids = [r.Election.election_id for r in rows]
    votes_by_election: dict = {}
    revenue_by_election: dict = {}
    if election_ids:
        vote_rows = await db.execute(
            select(Vote.election_id, sqlfunc.sum(Vote.count))
            .where(Vote.election_id.in_(election_ids))
            .group_by(Vote.election_id)
        )
        votes_by_election = {r[0]: int(r[1]) for r in vote_rows.all()}
        txn_rows = await db.execute(
            select(Transaction.election_id, sqlfunc.sum(Transaction.amount))
            .where(
                Transaction.election_id.in_(election_ids),
                Transaction.status == "success",
            )
            .group_by(Transaction.election_id)
        )
        revenue_by_election = {r[0]: int(r[1] or 0) for r in txn_rows.all()}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Elections"
    ws.append(["Title", "Organization", "Status", "Plan", "Visibility", "Categories", "Total Votes", "Revenue (GHS)", "Start Date", "End Date", "Created"])
    for r in rows:
        e = r.Election
        pesewas = revenue_by_election.get(e.election_id, 0)
        ws.append([
            e.title,
            r.org_name or "",
            e.status,
            e.plan,
            e.visibility,
            len(e.categories) if e.categories else 0,
            votes_by_election.get(e.election_id, 0),
            round(pesewas / 100, 2),
            _xl_dt(e.start_datetime),
            _xl_dt(e.end_datetime),
            _xl_dt(e.created_at),
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"elections_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Admin events ──────────────────────────────────────────────────────────────

class AdminEventListItem(BaseModel):
    event_id: UUID
    title: str
    slug: Optional[str] = None
    status: str
    plan: str
    visibility: str = "public"
    fee_payer: str
    banner_image_url: Optional[str] = None
    total_tickets_sold: int
    total_revenue_pesewas: int
    org_id: Optional[UUID] = None
    org_name: Optional[str] = None
    ticket_type_count: int
    event_date: date
    location: str
    commission_rate_override_bps: Optional[int] = None
    created_at: datetime


class AdminEventListResponse(BaseModel):
    items: List[AdminEventListItem]
    total: int
    skip: int
    limit: int


class AdminEventDetail(AdminEventListItem):
    description: Optional[str] = None
    event_time: str
    capacity: Optional[int] = None
    ticket_types: List[dict] = []


@router.get("/events", response_model=AdminEventListResponse)
async def list_all_events(
    status: Optional[str] = None,
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Platform-wide event list with org name and revenue aggregation."""
    from sqlalchemy import select, func as sqlfunc
    from app.models.event import Event, TicketType
    from app.models.organization import Organization
    from app.models.transaction import Transaction

    stmt = (
        select(Event, Organization.name.label("org_name"))
        .outerjoin(Organization, Organization.org_id == Event.org_id)
    )
    if status:
        stmt = stmt.where(Event.status == status)
    if search:
        stmt = stmt.where(Event.title.ilike(f"%{search}%"))

    count_stmt = select(sqlfunc.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(Event.created_at.desc()).offset(skip).limit(limit)
    rows = (await db.execute(stmt)).all()

    event_ids = [r.Event.event_id for r in rows]

    tickets_sold_by_event: dict[UUID, int] = {}
    revenue_by_event: dict[UUID, int] = {}
    ticket_type_count_by_event: dict[UUID, int] = {}
    if event_ids:
        tt_rows = await db.execute(
            select(TicketType.event_id, sqlfunc.sum(TicketType.quantity_sold), sqlfunc.count(TicketType.ticket_type_id))
            .where(TicketType.event_id.in_(event_ids))
            .group_by(TicketType.event_id)
        )
        for r in tt_rows.all():
            tickets_sold_by_event[r[0]] = int(r[1] or 0)
            ticket_type_count_by_event[r[0]] = int(r[2] or 0)

        txn_rows = await db.execute(
            select(Transaction.event_id, sqlfunc.sum(Transaction.amount))
            .where(
                Transaction.event_id.in_(event_ids),
                Transaction.status == "success",
            )
            .group_by(Transaction.event_id)
        )
        revenue_by_event = {r[0]: int(r[1] or 0) for r in txn_rows.all()}

    items = [
        AdminEventListItem(
            event_id=r.Event.event_id,
            title=r.Event.title,
            slug=r.Event.slug,
            status=r.Event.status,
            plan=r.Event.plan,
            fee_payer=r.Event.fee_payer,
            banner_image_url=r.Event.banner_image_url,
            total_tickets_sold=tickets_sold_by_event.get(r.Event.event_id, 0),
            total_revenue_pesewas=revenue_by_event.get(r.Event.event_id, 0),
            org_id=r.Event.org_id,
            org_name=r.org_name,
            ticket_type_count=ticket_type_count_by_event.get(r.Event.event_id, 0),
            event_date=r.Event.event_date,
            location=r.Event.location,
            commission_rate_override_bps=r.Event.commission_rate_override_bps,
            created_at=r.Event.created_at,
        )
        for r in rows
    ]

    return AdminEventListResponse(items=items, total=total, skip=skip, limit=limit)


@router.get("/events/export")
async def export_events_excel(
    status: Optional[str] = None,
    search: Optional[str] = None,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    import io, openpyxl
    from sqlalchemy import select, func as sqlfunc
    from app.models.event import Event, TicketType
    from app.models.organization import Organization
    from app.models.transaction import Transaction

    stmt = (
        select(Event, Organization.name.label("org_name"))
        .outerjoin(Organization, Organization.org_id == Event.org_id)
    )
    if status:
        stmt = stmt.where(Event.status == status)
    if search:
        stmt = stmt.where(Event.title.ilike(f"%{search}%"))
    stmt = stmt.order_by(Event.created_at.desc()).limit(10000)
    rows = (await db.execute(stmt)).all()

    event_ids = [r.Event.event_id for r in rows]
    tickets_sold_by_event: dict = {}
    revenue_by_event: dict = {}
    ticket_type_count_by_event: dict = {}
    if event_ids:
        tt_rows = await db.execute(
            select(TicketType.event_id, sqlfunc.sum(TicketType.quantity_sold), sqlfunc.count(TicketType.ticket_type_id))
            .where(TicketType.event_id.in_(event_ids))
            .group_by(TicketType.event_id)
        )
        for r in tt_rows.all():
            tickets_sold_by_event[r[0]] = int(r[1] or 0)
            ticket_type_count_by_event[r[0]] = int(r[2] or 0)
        txn_rows = await db.execute(
            select(Transaction.event_id, sqlfunc.sum(Transaction.amount))
            .where(Transaction.event_id.in_(event_ids), Transaction.status == "success")
            .group_by(Transaction.event_id)
        )
        revenue_by_event = {r[0]: int(r[1] or 0) for r in txn_rows.all()}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Events"
    ws.append(["Title", "Organisation", "Status", "Plan", "Location", "Tickets Sold", "Ticket Types", "Revenue (GHS)", "Event Date", "Created"])
    for r in rows:
        e = r.Event
        revenue_ghs = revenue_by_event.get(e.event_id, 0) / 100
        ws.append([
            e.title,
            r.org_name or "",
            e.status,
            e.plan,
            e.location,
            tickets_sold_by_event.get(e.event_id, 0),
            ticket_type_count_by_event.get(e.event_id, 0),
            round(revenue_ghs, 2),
            _xl_dt(e.event_date) if isinstance(e.event_date, datetime) else e.event_date,
            _xl_dt(e.created_at),
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"events_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/events/{event_id}", response_model=AdminEventDetail)
async def get_admin_event_detail(
    event_id: UUID,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Full event detail for admin drawer."""
    from sqlalchemy import select, func as sqlfunc
    from sqlalchemy.orm import selectinload
    from app.models.event import Event, TicketType
    from app.models.organization import Organization
    from app.models.transaction import Transaction

    stmt = (
        select(Event, Organization.name.label("org_name"))
        .outerjoin(Organization, Organization.org_id == Event.org_id)
        .options(selectinload(Event.ticket_types))
        .where(Event.event_id == event_id)
    )
    row = (await db.execute(stmt)).first()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")

    event, org_name = row.Event, row.org_name

    txn_result = await db.execute(
        select(sqlfunc.sum(Transaction.amount))
        .where(Transaction.event_id == event_id, Transaction.status == "success")
    )
    total_revenue = int(txn_result.scalar_one_or_none() or 0)
    total_tickets_sold = sum(tt.quantity_sold for tt in event.ticket_types)

    return AdminEventDetail(
        event_id=event.event_id,
        title=event.title,
        slug=event.slug,
        description=event.description,
        status=event.status,
        plan=event.plan,
        fee_payer=event.fee_payer,
        banner_image_url=event.banner_image_url,
        total_tickets_sold=total_tickets_sold,
        total_revenue_pesewas=total_revenue,
        org_id=event.org_id,
        org_name=org_name,
        ticket_type_count=len(event.ticket_types),
        event_date=event.event_date,
        event_time=str(event.event_time),
        location=event.location,
        capacity=event.capacity,
        created_at=event.created_at,
        ticket_types=[
            {
                "ticket_type_id": str(tt.ticket_type_id),
                "type_name": tt.type_name,
                "price": int(tt.price * 100),
                "quantity_available": tt.quantity_available,
                "quantity_sold": tt.quantity_sold,
                "status": tt.status,
            }
            for tt in sorted(event.ticket_types, key=lambda t: t.price)
        ],
    )


class EventCommissionOverride(BaseModel):
    commission_rate_override_bps: Optional[int] = None  # None = clear override


class EventCommissionOverrideResponse(BaseModel):
    event_id: UUID
    commission_rate_override_bps: Optional[int]
    effective_commission_bps: int


@router.put("/events/{event_id}/commission-rate", response_model=EventCommissionOverrideResponse)
async def override_event_commission_rate(
    event_id: UUID,
    data: EventCommissionOverride,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    """Set (or clear) a per-event commission rate override in basis points."""
    from sqlalchemy import select
    from app.models.event import Event
    from app.core.plans import commission_rate_bps, TICKET_COMMISSION_RATE_BPS

    result = await db.execute(select(Event).where(Event.event_id == event_id))
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    old_bps = event.commission_rate_override_bps
    event.commission_rate_override_bps = data.commission_rate_override_bps
    await db.commit()
    await db.refresh(event)

    effective = commission_rate_bps(
        event.plan, event.commission_rate_override_bps, rate_table=TICKET_COMMISSION_RATE_BPS
    )

    await AuditLogRepository(AuditLog, db).log_action(
        action_type="OVERRIDE_EVENT_COMMISSION_RATE",
        entity_type="Event",
        entity_id=event_id,
        user_id=current_user.user_id,
        changes={"commission_rate_override_bps": {"old": old_bps, "new": data.commission_rate_override_bps}},
    )

    return EventCommissionOverrideResponse(
        event_id=event.event_id,
        commission_rate_override_bps=event.commission_rate_override_bps,
        effective_commission_bps=effective,
    )


@router.get("/elections/{election_id}", response_model=AdminElectionDetail)
async def get_election_detail(
    election_id: UUID,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Full admin detail for one election: categories with vote counts, revenue, voter count."""
    from sqlalchemy import select, func as sqlfunc
    from sqlalchemy.orm import selectinload
    from app.models.election import Election, Category, EligibleVoter
    from app.models.organization import Organization
    from app.models.vote import Vote
    from app.models.transaction import Transaction

    result = await db.execute(
        select(Election)
        .options(selectinload(Election.categories).selectinload(Category.candidates))
        .where(Election.election_id == election_id)
    )
    election = result.scalar_one_or_none()
    if not election:
        raise HTTPException(status_code=404, detail="Election not found")

    org_name: Optional[str] = None
    if election.org_id:
        org = await db.get(Organization, election.org_id)
        org_name = org.name if org else None

    vote_rows = await db.execute(
        select(Vote.category_id, sqlfunc.sum(Vote.count))
        .where(Vote.election_id == election_id)
        .group_by(Vote.category_id)
    )
    votes_by_category: dict[UUID, int] = {r[0]: int(r[1]) for r in vote_rows.all()}

    txn_row = await db.execute(
        select(sqlfunc.sum(Transaction.amount))
        .where(
            Transaction.election_id == election_id,
            Transaction.status == "success",
        )
    )
    total_revenue = int(txn_row.scalar_one() or 0)
    total_votes = sum(votes_by_category.values())

    voter_count_row = await db.execute(
        select(sqlfunc.count(EligibleVoter.eligible_voter_id))
        .where(EligibleVoter.election_id == election_id)
    )
    eligible_voter_count = int(voter_count_row.scalar_one() or 0)

    global_price = int(await _get_platform_setting(db, "vote_price"))

    category_details = [
        AdminElectionCategoryDetail(
            category_id=cat.category_id,
            name=cat.name,
            election_type=cat.election_type,
            candidate_count=len(cat.candidates),
            total_votes=votes_by_category.get(cat.category_id, 0),
            # vote_data is encrypted so we cannot rank candidates here; None
            # keeps the field in the schema without exposing a wrong answer.
            top_candidate_name=None,
        )
        for cat in sorted(election.categories, key=lambda c: c.display_order)
    ]

    return AdminElectionDetail(
        election_id=election.election_id,
        title=election.title,
        slug=election.slug,
        description=election.description,
        status=election.status,
        plan=election.plan,
        visibility=election.visibility,
        fee_payer=election.fee_payer,
        banner_image_url=election.banner_image_url,
        vote_price=election.vote_price,
        effective_vote_price=(
            election.vote_price if election.vote_price is not None else global_price
        ),
        total_votes=total_votes,
        total_revenue_pesewas=total_revenue,
        org_id=election.org_id,
        org_name=org_name,
        category_count=len(election.categories),
        start_datetime=election.start_datetime,
        end_datetime=election.end_datetime,
        created_at=election.created_at,
        eligible_voter_count=eligible_voter_count,
        categories=category_details,
    )


# ── Admin transactions ─────────────────────────────────────────────────────────

class AdminTransactionItem(BaseModel):
    transaction_id: str
    reference: str
    kind: str
    parent_type: Optional[str] = None
    parent_title: Optional[str] = None
    org_name: Optional[str] = None
    email: Optional[str] = None
    amount_ghs: float
    status: str
    fee_payer: str
    vote_count: Optional[int] = None
    created_at: str


class AdminTransactionsResponse(BaseModel):
    items: List[AdminTransactionItem]
    total: int
    total_revenue_ghs: float
    vote_revenue_ghs: float
    ticket_revenue_ghs: float
    success_count: int
    failed_count: int
    pending_count: int


def _txn_period_start(period: str) -> Optional[datetime]:
    """Return UTC start datetime for a period string."""
    now = datetime.now(timezone.utc)
    if period == "7d":
        return now - timedelta(days=7)
    if period == "30d":
        return now - timedelta(days=30)
    if period == "90d":
        return now - timedelta(days=90)
    if period.startswith("y:"):
        year = int(period[2:])
        return datetime(year, 1, 1, tzinfo=timezone.utc)
    return None


async def _fetch_all_transactions(
    db: AsyncSession,
    period: str,
    kind: Optional[str],
    status: Optional[str],
    search: Optional[str],
) -> list[AdminTransactionItem]:
    """Fetch and combine vote + ticket transactions for the given filters."""
    from sqlalchemy import func, or_, select
    from app.models.election import Election
    from app.models.event import Event
    from app.models.ticket_transaction import TicketTransaction
    from app.models.transaction import Transaction

    start = _txn_period_start(period)
    items: list[AdminTransactionItem] = []

    if kind in (None, "vote"):
        stmt = (
            select(
                Transaction.transaction_id,
                Transaction.reference,
                Transaction.election_id,
                Transaction.event_id,
                Transaction.email,
                Transaction.amount,
                Transaction.status,
                Transaction.fee_payer,
                Transaction.vote_count,
                Transaction.created_at,
                Election.title.label("election_title"),
                Event.title.label("event_title"),
                Organization.name.label("org_name"),
            )
            .outerjoin(Election, Election.election_id == Transaction.election_id)
            .outerjoin(Event, Event.event_id == Transaction.event_id)
            .outerjoin(
                Organization,
                Organization.org_id == func.coalesce(Election.org_id, Event.org_id),
            )
        )
        if start:
            stmt = stmt.where(Transaction.created_at >= start)
        if status:
            stmt = stmt.where(Transaction.status == status)
        if search:
            stmt = stmt.where(
                or_(
                    Transaction.reference.ilike(f"%{search}%"),
                    Transaction.email.ilike(f"%{search}%"),
                    Election.title.ilike(f"%{search}%"),
                    Event.title.ilike(f"%{search}%"),
                    Organization.name.ilike(f"%{search}%"),
                )
            )
        for r in (await db.execute(stmt)).all():
            parent_type = "election" if r.election_id else "event"
            parent_title = r.election_title if r.election_id else r.event_title
            items.append(
                AdminTransactionItem(
                    transaction_id=str(r.transaction_id),
                    reference=r.reference,
                    kind="vote",
                    parent_type=parent_type,
                    parent_title=parent_title,
                    org_name=r.org_name,
                    email=r.email,
                    amount_ghs=round(r.amount / 100, 2),
                    status=r.status,
                    fee_payer=r.fee_payer,
                    vote_count=r.vote_count,
                    created_at=r.created_at.isoformat(),
                )
            )

    if kind in (None, "ticket"):
        stmt2 = (
            select(
                TicketTransaction.transaction_id,
                TicketTransaction.reference,
                TicketTransaction.event_id,
                TicketTransaction.guest_email,
                TicketTransaction.amount,
                TicketTransaction.status,
                TicketTransaction.fee_payer,
                TicketTransaction.created_at,
                Event.title.label("event_title"),
                Organization.name.label("org_name"),
            )
            .outerjoin(Event, Event.event_id == TicketTransaction.event_id)
            .outerjoin(Organization, Organization.org_id == Event.org_id)
        )
        if start:
            stmt2 = stmt2.where(TicketTransaction.created_at >= start)
        if status:
            stmt2 = stmt2.where(TicketTransaction.status == status)
        if search:
            stmt2 = stmt2.where(
                or_(
                    TicketTransaction.reference.ilike(f"%{search}%"),
                    TicketTransaction.guest_email.ilike(f"%{search}%"),
                    Event.title.ilike(f"%{search}%"),
                    Organization.name.ilike(f"%{search}%"),
                )
            )
        for r in (await db.execute(stmt2)).all():
            items.append(
                AdminTransactionItem(
                    transaction_id=str(r.transaction_id),
                    reference=r.reference,
                    kind="ticket",
                    parent_type="event",
                    parent_title=r.event_title,
                    org_name=r.org_name,
                    email=r.guest_email,
                    amount_ghs=float(r.amount),
                    status=r.status,
                    fee_payer=r.fee_payer,
                    vote_count=None,
                    created_at=r.created_at.isoformat(),
                )
            )

    items.sort(key=lambda x: x.created_at, reverse=True)
    return items


@router.get("/transactions", response_model=AdminTransactionsResponse)
async def list_admin_transactions(
    period: str = "30d",
    kind: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 500,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Platform-wide transaction list combining vote and ticket payments."""
    items = await _fetch_all_transactions(db, period, kind, status, search)

    vote_rev = sum(i.amount_ghs for i in items if i.kind == "vote" and i.status == "success")
    ticket_rev = sum(i.amount_ghs for i in items if i.kind == "ticket" and i.status == "success")

    return AdminTransactionsResponse(
        items=items[skip : skip + limit],
        total=len(items),
        total_revenue_ghs=round(vote_rev + ticket_rev, 2),
        vote_revenue_ghs=round(vote_rev, 2),
        ticket_revenue_ghs=round(ticket_rev, 2),
        success_count=sum(1 for i in items if i.status == "success"),
        failed_count=sum(1 for i in items if i.status in ("failed", "abandoned", "needs_refund")),
        pending_count=sum(1 for i in items if i.status == "pending"),
    )


@router.get("/transactions/export")
async def export_transactions_excel(
    period: str = "30d",
    kind: Optional[str] = None,
    status: Optional[str] = None,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Download an Excel export of all transactions for the selected period."""
    import io
    import openpyxl
    from fastapi.responses import StreamingResponse

    items = await _fetch_all_transactions(db, period, kind, status, None)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Transactions"
    ws.append(["Reference", "Type", "Parent", "Organisation", "Email", "Amount (GHS)", "Status", "Fee Payer", "Votes", "Date"])
    for item in items:
        ws.append([
            item.reference,
            item.kind.capitalize(),
            item.parent_title or "",
            item.org_name or "",
            item.email or "",
            item.amount_ghs,
            item.status,
            item.fee_payer,
            item.vote_count if item.vote_count is not None else "",
            _xl_dt(item.created_at),
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"transactions_{period}_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Admin sessions ────────────────────────────────────────────────────────────

@router.get("/sessions", response_model=List[AdminSessionResponse])
async def list_all_sessions(
    skip: int = 0,
    limit: int = 200,
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    current_session_id: Optional[str] = Depends(get_current_session_id),
    db: AsyncSession = Depends(get_db),
):
    """Every active session on the platform — for the admin Security page."""
    repo = RefreshTokenRepository(RefreshToken, db)
    tokens = await repo.get_all_active(skip=skip, limit=limit)
    return [
        AdminSessionResponse(
            jti=t.jti,
            ip_address=t.ip_address,
            user_agent=t.user_agent,
            created_at=t.created_at,
            expires_at=t.expires_at,
            is_current=(t.jti == current_session_id),
            user_id=t.user_id,
            user_name=t.user.full_name if t.user else None,
            user_email=t.user.email if t.user else None,
        )
        for t in tokens
    ]


@router.delete("/sessions/{jti}", status_code=204)
async def revoke_any_session(
    jti: str,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    """Force-revoke any session on the platform — kicks the device immediately."""
    repo = RefreshTokenRepository(RefreshToken, db)
    token = await repo.get_by_jti(jti)
    if not token:
        raise HTTPException(status_code=404, detail="Session not found")
    target_user_id = str(token.user_id)
    await repo.revoke(jti)
    await revoke_session_in_redis(jti, ttl_seconds=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    await AuditLogRepository(AuditLog, db).log_action(
        action_type="REVOKE_SESSION",
        entity_type="RefreshToken",
        entity_id=None,
        user_id=current_user.user_id,
        changes={"jti": jti, "target_user_id": target_user_id},
    )


# ── Abuse Alerts ─────────────────────────────────────────────────────────────

from app.models.abuse_alert import AbuseAlert as AbuseAlertModel, AlertSeverity, AlertStatus, AlertType
from app.repositories.abuse_alert_repository import AbuseAlertRepository
from app.schemas.abuse_alert import AbuseAlertResponse, AbuseAlertStats, AbuseAlertUpdate


def _alert_to_response(a: AbuseAlertModel) -> AbuseAlertResponse:
    return AbuseAlertResponse(
        id=a.id,
        alert_type=a.alert_type,
        severity=a.severity,
        source_ip=a.source_ip,
        source_user_id=a.source_user_id,
        source_user_name=a.source_user.full_name if a.source_user else None,
        source_user_email=a.source_user.email if a.source_user else None,
        target_path=a.target_path,
        detail=a.detail,
        status=a.status,
        created_at=a.created_at,
        resolved_at=a.resolved_at,
        resolved_by_name=a.resolver.full_name if a.resolver else None,
    )


@router.get("/abuse-alerts/stats", response_model=AbuseAlertStats)
async def get_abuse_alert_stats(
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate counts for the Abuse Alerts dashboard header."""
    repo = AbuseAlertRepository(db)
    stats = await repo.get_stats()
    return AbuseAlertStats(**stats)


@router.get("/abuse-alerts", response_model=List[AbuseAlertResponse])
async def list_abuse_alerts(
    current_user: User = Depends(require_roles(*CONSOLE_READ_ROLES)),
    db: AsyncSession = Depends(get_db),
    severity: Optional[str] = None,
    alert_type: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
):
    """Filtered list of abuse alerts, newest first."""
    from datetime import datetime, timezone, timedelta

    since_dt: Optional[datetime] = None
    if since == "24h":
        since_dt = datetime.now(timezone.utc) - timedelta(hours=24)
    elif since == "7d":
        since_dt = datetime.now(timezone.utc) - timedelta(days=7)
    elif since == "30d":
        since_dt = datetime.now(timezone.utc) - timedelta(days=30)

    repo = AbuseAlertRepository(db)
    alerts = await repo.list_filtered(
        severity=AlertSeverity(severity) if severity else None,
        alert_type=AlertType(alert_type) if alert_type else None,
        status=AlertStatus(status) if status else None,
        since=since_dt,
        skip=skip,
        limit=limit,
    )
    return [_alert_to_response(a) for a in alerts]


@router.patch("/abuse-alerts/{alert_id}", response_model=AbuseAlertResponse)
async def update_abuse_alert_status(
    alert_id: UUID,
    data: AbuseAlertUpdate,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    """Update the status of an abuse alert (investigate, resolve, dismiss)."""
    repo = AbuseAlertRepository(db)
    alert = await repo.update_status(alert_id, data.status, resolved_by_id=current_user.user_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    await AuditLogRepository(AuditLog, db).log_action(
        action_type="UPDATE_ABUSE_ALERT",
        entity_type="AbuseAlert",
        entity_id=alert_id,
        user_id=current_user.user_id,
        changes={"status": data.status},
    )
    return _alert_to_response(alert)
