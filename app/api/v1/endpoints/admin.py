from datetime import date, datetime, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_roles
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.platform_setting import PlatformSetting
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

    @field_validator("vote_price")
    @classmethod
    def validate_vote_price(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return v
        if v < 100:
            raise ValueError("Global vote price must be at least 100 pesewas (₵1)")
        if v % 100 != 0:
            raise ValueError("Global vote price must be a multiple of 100 pesewas")
        return v

    @field_validator("max_candidates_per_election")
    @classmethod
    def validate_max_candidates(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 0:
            raise ValueError("Max candidates must be 0 (unlimited) or a positive integer")
        return v


async def _get_platform_setting(db: AsyncSession, key: str) -> str:
    from sqlalchemy import select
    result = await db.execute(select(PlatformSetting).where(PlatformSetting.key == key))
    row = result.scalar_one_or_none()
    return row.value if row else PLATFORM_SETTING_DEFAULTS.get(key, "")


async def _set_platform_setting(db: AsyncSession, key: str, value: str, user_id) -> None:
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
    )


@router.get("/platform-settings", response_model=PlatformSettingsResponse)
async def get_platform_settings(
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    return await _fetch_all_settings(db)


@router.put("/platform-settings", response_model=PlatformSettingsResponse)
async def update_platform_settings(
    data: PlatformSettingsUpdate,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
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
        if v is None:
            return v
        if v < 100:
            raise ValueError("Vote price must be at least 100 pesewas (₵1)")
        if v % 100 != 0:
            raise ValueError("Vote price must be a multiple of 100 pesewas")
        return v


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
    elections: List[ElectionAnalytics]
    events: List[EventAnalytics]


@router.get("/organizations/{org_id}/analytics", response_model=OrgAnalyticsResponse)
async def get_organization_analytics(
    org_id: UUID,
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
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

    # Collect all member user_ids for this org
    member_result = await db.execute(
        select(OrganizationMember.user_id).where(OrganizationMember.org_id == org_id)
    )
    user_ids = [row[0] for row in member_result.all()]

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
        elections=election_analytics,
        events=event_analytics,
    )
