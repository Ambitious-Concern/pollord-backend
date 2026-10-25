from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_current_active_user
from app.core.ownership import SYSTEM_ADMIN
from app.core.org_permissions import MANAGE_SETTINGS, MANAGE_TEAM, member_has_permission
from app.core.request_meta import client_ip_and_agent
from app.core.security import hash_password
from app.db.base import get_db
from app.models.audit_log import AuditLog
from app.models.election import Election
from app.models.event import Event
from app.models.org_activity_log import OrgActivityLog
from app.models.organization import Organization, OrganizationMember, OrgRole
from app.models.payout_request import PayoutRequest
from app.models.user import User
from app.models.vote import Vote
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.election_repository import ElectionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.org_activity_log_repository import OrgActivityLogRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.org_role_repository import OrgRoleRepository
from app.repositories.payout_request_repository import PayoutRequestRepository
from app.repositories.user_repository import UserRepository
from app.repositories.vote_repository import VoteRepository
from app.schemas.organization import (
    AcceptInvitationRequest,
    AcceptInvitationSignupRequest,
    OrganizationCreate,
    OrganizationInvitationResponse,
    OrganizationMemberAdd,
    OrganizationMemberResponse,
    OrganizationMemberUpdate,
    OrganizationResponse,
    OrganizationUpdate,
    OrgRoleCreate,
    OrgRoleResponse,
    OrgRoleUpdate,
    TransferOwnershipRequest,
)
from app.schemas.user import TokenResponse
from app.services import email_service
from app.services.auth_service import AuthService
from app.services.file_storage_service import file_storage_service
from app.services.org_notification_service import OrgNotificationService

# System roles granted to org owners and admins so they can create elections/events
# The organizer app gates its whole dashboard on these, so every member
# needs them just to see the organization's work. Write access is a separate
# question, answered by OrganizationRepository.MANAGING_ROLES.
ORG_MEMBER_SYSTEM_ROLES = ["Election Administrator", "Event Organizer"]


from pydantic import field_validator as _fv


class OrganizationMemberInvite(BaseModel):
    email: EmailStr
    role: str = "member"

    @_fv("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        allowed = {"admin", "editor", "member"}
        if v not in allowed:
            raise ValueError(f"Role must be one of: {allowed}")
        return v

router = APIRouter(prefix="/organizations", tags=["Organizations"])


class OrganizationStatsResponse(BaseModel):
    """Aggregate public stats for an org's profile page (e.g. v2's organiser card)."""
    org_id: UUID
    elections_count: int
    events_count: int
    total_votes_cast: int
    member_since_year: int


class OrganizationPublicResponse(BaseModel):
    """Subset of org data safe to expose without authentication."""
    org_id: UUID
    name: str
    description: Optional[str] = None
    logo_url: Optional[str] = None
    brand_primary_color: Optional[str] = None
    website: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    industry: Optional[str] = None
    is_verified: bool
    owner_id: UUID
    owner_name: str
    created_at: datetime
    member_count: int


def _member_to_response(m: OrganizationMember) -> OrganizationMemberResponse:
    return OrganizationMemberResponse(
        member_id=m.member_id,
        org_id=m.org_id,
        user_id=m.user_id,
        role=m.role,
        joined_at=m.joined_at,
        user_name=m.user.full_name if m.user else None,
        user_email=m.user.email if m.user else None,
        org_role=OrgRoleResponse.model_validate(m.org_role) if m.org_role else None,
    )


def _org_to_response(
    org: Organization,
    *,
    viewer: User | None = None,
) -> OrganizationResponse:
    """Serialize an organization.

    KYC document URLs are redacted unless ``viewer`` is the owner or a System
    Administrator. They point at identity documents, so handing them to every
    teammate who can read the org would widen access far beyond who actually
    needs to see them. Redacting by default means a new call site has to opt in
    deliberately rather than leak them by omission.
    """
    may_see_kyc = viewer is not None and (
        org.owner_id == viewer.user_id
        or SYSTEM_ADMIN in [ur.role.role_name for ur in viewer.user_roles]
    )
    return OrganizationResponse(
        org_id=org.org_id,
        name=org.name,
        description=org.description,
        logo_url=org.logo_url,
        brand_primary_color=org.brand_primary_color,
        website=org.website,
        address=org.address,
        phone=org.phone,
        email=org.email,
        industry=org.industry,
        is_verified=org.is_verified,
        kyc_document_front=org.kyc_document_front if may_see_kyc else None,
        kyc_document_back=org.kyc_document_back if may_see_kyc else None,
        owner_id=org.owner_id,
        is_active=org.is_active,
        deactivated_at=org.deactivated_at,
        created_at=org.created_at,
        updated_at=org.updated_at,
        members=[_member_to_response(m) for m in (org.members or [])],
    )


# --- KYC: Create organization ---


@router.post("", response_model=OrganizationResponse, status_code=201)
async def create_organization(
    data: OrganizationCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Create an organization (KYC step). The user becomes the owner."""
    repo = OrganizationRepository(Organization, db)

    payload = data.model_dump()
    # If the client sent the logo as a base64 data URI, upload it and store the URL.
    payload["logo_url"] = await file_storage_service.resolve(
        payload.get("logo_url"), filename_hint="logo"
    )

    org = await repo.create(
        {
            **payload,
            "owner_id": current_user.user_id,
        }
    )

    # Auto-add the creator as owner member
    await repo.add_member(
        org_id=org.org_id,
        user_id=current_user.user_id,
        role="owner",
    )

    # Grant system roles so the owner can create elections and events
    user_repo = UserRepository(User, db)
    await user_repo.grant_roles_by_name(current_user.user_id, ORG_MEMBER_SYSTEM_ROLES)

    org = await repo.get_with_members(org.org_id)
    return _org_to_response(org, viewer=current_user)


# --- List user's organizations ---


@router.get("/my", response_model=List[OrganizationResponse])
async def list_my_organizations(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """List all organizations the user owns or is a member of."""
    repo = OrganizationRepository(Organization, db)
    orgs = await repo.get_user_organizations(current_user.user_id)
    return [_org_to_response(o, viewer=current_user) for o in orgs if o.is_active]


# --- Public org info by owner user_id (no auth required) ---
# The public share URL uses /:userId/explore where userId is the owner's user_id.


@router.get("/owner/{user_id}", response_model=OrganizationPublicResponse)
async def get_org_by_owner(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Return the organization owned by a specific user. No auth required."""
    repo = OrganizationRepository(Organization, db)
    orgs = [o for o in await repo.get_by_owner(user_id) if o.is_active]
    if not orgs:
        raise HTTPException(status_code=404, detail="Organization not found for this user")
    org = orgs[0]
    owner = await UserRepository(User, db).get_by_id(user_id, id_field="user_id")

    return OrganizationPublicResponse(
        org_id=org.org_id,
        name=org.name,
        description=org.description,
        logo_url=org.logo_url,
        brand_primary_color=org.brand_primary_color,
        website=org.website,
        address=org.address,
        phone=org.phone,
        email=org.email,
        industry=org.industry,
        is_verified=org.is_verified,
        owner_id=org.owner_id,
        owner_name=owner.full_name if owner else "",
        created_at=org.created_at,
        member_count=len(org.members or []),
    )


# --- Public org info by org_id (no auth required) ---


@router.get("/public/{org_id}", response_model=OrganizationPublicResponse)
async def get_public_organization(
    org_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Return basic organization info by org_id publicly."""
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org or not org.is_active:
        raise HTTPException(status_code=404, detail="Organization not found")
    owner = await UserRepository(User, db).get_by_id(org.owner_id, id_field="user_id")

    return OrganizationPublicResponse(
        org_id=org.org_id,
        name=org.name,
        description=org.description,
        logo_url=org.logo_url,
        brand_primary_color=org.brand_primary_color,
        website=org.website,
        address=org.address,
        phone=org.phone,
        email=org.email,
        industry=org.industry,
        is_verified=org.is_verified,
        owner_id=org.owner_id,
        owner_name=owner.full_name if owner else "",
        created_at=org.created_at,
        member_count=len(org.members or []),
    )


# --- Public org stats by org_id (no auth required) ---


@router.get("/{org_id}/stats", response_model=OrganizationStatsResponse)
async def get_organization_stats(
    org_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Aggregate elections/events run and total votes cast, scoped to everyone
    on the org's team — elections/events carry no org_id, only created_by, so
    "the organization's data" means anything created by any teammate, not
    just the literal owner account (same scoping list_elections/list_events
    already use for the authenticated dashboard)."""
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org or not org.is_active:
        raise HTTPException(status_code=404, detail="Organization not found")

    teammate_ids = await repo.get_teammate_ids(org.owner_id)
    election_repo = ElectionRepository(Election, db)
    event_repo = EventRepository(Event, db)
    elections = await election_repo.get_elections_by_creators(teammate_ids, limit=10_000)
    events = await event_repo.get_events_by_creators(teammate_ids, limit=10_000)

    vote_repo = VoteRepository(Vote, db)
    total_votes = 0
    for election in elections:
        total_votes += await vote_repo.count_by_election(election.election_id)
    for event in events:
        event_votes = await vote_repo.get_votes_by_event(event.event_id)
        total_votes += sum(getattr(v, "count", 1) for v in event_votes)

    return OrganizationStatsResponse(
        org_id=org.org_id,
        elections_count=len(elections),
        events_count=len(events),
        total_votes_cast=total_votes,
        member_since_year=org.created_at.year,
    )


# --- Get single organization ---


@router.get("/{org_id}", response_model=OrganizationResponse)
async def get_organization(
    org_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Full organization detail — caller must be a member."""
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org or not org.is_active:
        raise HTTPException(status_code=404, detail="Organization not found")

    is_member = any(m.user_id == current_user.user_id for m in org.members)
    if not is_member:
        raise HTTPException(status_code=403, detail="Not a member of this organization")

    return _org_to_response(org, viewer=current_user)


# --- Update organization ---


@router.put("/{org_id}", response_model=OrganizationResponse)
async def update_organization(
    request: Request,
    org_id: UUID,
    data: OrganizationUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Partial update, restricted to the org's owner/admin members."""
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    member = next((m for m in org.members if m.user_id == current_user.user_id), None)
    if not member or not (member.role in ("owner", "admin") or member_has_permission(member, MANAGE_SETTINGS)):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    update_data = data.model_dump(exclude_unset=True)
    # Custom branding is open to every plan.
    # Convert a base64 logo data URI to a stored URL before persisting.
    if "logo_url" in update_data:
        update_data["logo_url"] = await file_storage_service.resolve(
            update_data["logo_url"], filename_hint="logo"
        )

    updated = await repo.update(org_id, update_data, id_field="org_id")
    org = await repo.get_with_members(org_id)
    if update_data:
        ip, agent = client_ip_and_agent(request)
        await OrgActivityLogRepository(OrgActivityLog, db).log_activity(
            org_id=org_id,
            category="settings",
            verb="updated",
            target_name="Organization profile",
            user_id=current_user.user_id,
            detail=f"Updated: {', '.join(sorted(update_data.keys()))}",
            ip_address=ip,
            user_agent=agent,
        )
    return _org_to_response(org, viewer=current_user)


# --- Add member ---


@router.post("/{org_id}/members", response_model=OrganizationMemberResponse, status_code=201)
async def add_member(
    org_id: UUID,
    data: OrganizationMemberAdd,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Add an existing user as a member directly (no invitation email —
    see invite_member_by_email for that flow)."""
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    member = next((m for m in org.members if m.user_id == current_user.user_id), None)
    if not member or not (member.role in ("owner", "admin") or member_has_permission(member, MANAGE_TEAM)):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    existing = await repo.get_member(org_id, data.user_id)
    if existing:
        raise HTTPException(status_code=409, detail="User is already a member")

    existing_member_ids = [m.user_id for m in org.members]

    new_member = await repo.add_member(
        org_id=org_id,
        user_id=data.user_id,
        role=data.role,
        invited_by=current_user.user_id,
    )

    # Every member needs these to reach the organizer dashboard at all.
    user_repo = UserRepository(User, db)
    await user_repo.grant_roles_by_name(data.user_id, ORG_MEMBER_SYSTEM_ROLES)

    added_user = await user_repo.get_by_id(data.user_id, id_field="user_id")
    await OrgNotificationService(db).notify(
        org_id=org_id,
        member_ids=existing_member_ids,
        pref_key="new_member",
        notif_type="alert",
        title=f"{added_user.full_name if added_user else 'A new member'} joined your team",
        description=f"Added as {data.role}.",
        link="/dashboard/settings",
    )

    return OrganizationMemberResponse(
        member_id=new_member.member_id,
        org_id=new_member.org_id,
        user_id=new_member.user_id,
        role=new_member.role,
        joined_at=new_member.joined_at,
    )


# --- Invite member by email ---


@router.post("/{org_id}/invite", status_code=201)
async def invite_member_by_email(
    request: Request,
    org_id: UUID,
    data: OrganizationMemberInvite,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Invite someone to the organization by email.
    - If they already have an account → add them immediately.
    - If they don't → create a pending invitation and send them an email with a
      signup link containing the token so they can join after onboarding.
    """
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    caller = next((m for m in org.members if m.user_id == current_user.user_id), None)
    if not caller or not (caller.role in ("owner", "admin") or member_has_permission(caller, MANAGE_TEAM)):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    user_repo = UserRepository(User, db)
    target = await user_repo.get_by_email(data.email)

    if target:
        # ── User already exists — add them directly ──────────────────────────
        existing = await repo.get_member(org_id, target.user_id)
        if existing:
            raise HTTPException(
                status_code=409, detail="User is already a member of this organization"
            )

        existing_member_ids = [m.user_id for m in org.members]

        new_member = await repo.add_member(
            org_id=org_id,
            user_id=target.user_id,
            role=data.role,
            invited_by=current_user.user_id,
        )
        if data.role == "admin":
            await user_repo.grant_roles_by_name(target.user_id, ORG_MEMBER_SYSTEM_ROLES)

        ip, agent = client_ip_and_agent(request)
        await OrgActivityLogRepository(OrgActivityLog, db).log_activity(
            org_id=org_id,
            category="team",
            verb="added",
            target_name=f"{target.full_name} as {data.role}",
            user_id=current_user.user_id,
            ip_address=ip,
            user_agent=agent,
        )
        await OrgNotificationService(db).notify(
            org_id=org_id,
            member_ids=existing_member_ids,
            pref_key="new_member",
            notif_type="alert",
            title=f"{target.full_name} joined your team",
            description=f"Added as {data.role}.",
            link="/dashboard/settings",
        )

        return {
            "type": "added",
            "member_id": str(new_member.member_id),
            "org_id": str(new_member.org_id),
            "user_id": str(new_member.user_id),
            "role": new_member.role,
            "joined_at": new_member.joined_at.isoformat(),
            "user_name": target.full_name,
            "user_email": target.email,
        }

    # ── User does not exist yet — create a pending invitation ────────────────
    # Check for an already-pending invitation to avoid spamming
    existing_invite = await repo.get_pending_invitation(org_id, data.email)
    if existing_invite:
        raise HTTPException(
            status_code=409,
            detail="An invitation has already been sent to that email address",
        )

    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    invitation = await repo.create_invitation(
        org_id=org_id,
        email=data.email,
        role=data.role,
        invited_by=current_user.user_id,
        expires_at=expires_at,
    )

    accept_url = (
        f"{settings.FRONTEND_URL}/invite/accept?token={invitation.token}"
    )
    subject, html_body = email_service.org_invitation_email(
        org_name=org.name,
        inviter_name=current_user.full_name,
        role=data.role,
        accept_url=accept_url,
    )
    email_service.send_email(data.email, subject, html_body)

    ip, agent = client_ip_and_agent(request)
    await OrgActivityLogRepository(OrgActivityLog, db).log_activity(
        org_id=org_id,
        category="team",
        verb="invited",
        target_name=f"a new {data.role}",
        user_id=current_user.user_id,
        detail=data.email,
        ip_address=ip,
        user_agent=agent,
    )

    return {
        "type": "invited",
        "invitation_id": str(invitation.invitation_id),
        "org_id": str(invitation.org_id),
        "email": invitation.email,
        "role": invitation.role,
        "expires_at": invitation.expires_at.isoformat(),
    }


# --- Get invitation details (public — no auth required) ---


@router.get("/invitations/{token}", response_model=OrganizationInvitationResponse)
async def get_invitation(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """Return invitation details so the frontend can show org name / role before the user signs up."""
    repo = OrganizationRepository(Organization, db)
    inv = await repo.get_invitation_by_token(token)
    if not inv:
        raise HTTPException(status_code=404, detail="Invitation not found or already used")
    if inv.status != "pending":
        raise HTTPException(status_code=410, detail="Invitation has already been used or expired")
    if inv.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Invitation has expired")

    return OrganizationInvitationResponse(
        invitation_id=inv.invitation_id,
        org_id=inv.org_id,
        org_name=inv.organization.name,
        email=inv.email,
        role=inv.role,
        status=inv.status,
        expires_at=inv.expires_at,
        created_at=inv.created_at,
    )


# --- Accept invitation (requires auth — user must be logged in / just signed up) ---


@router.post(
    "/invitations/accept-with-signup",
    response_model=TokenResponse,
    status_code=201,
)
async def accept_invitation_with_signup(
    data: AcceptInvitationSignupRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create an account from an invitation and join, in one step.

    Unauthenticated by design: the invitation token is the credential. It was
    emailed to the address it carries, so possession of it proves control of
    that inbox — which is why the account is created email_verified and skips
    the OTP the normal signup path uses. The invitee supplies only a name and
    a password; the email comes from the invitation.
    """
    repo = OrganizationRepository(Organization, db)
    inv = await repo.get_invitation_by_token(data.token)
    if not inv:
        raise HTTPException(status_code=404, detail="Invitation not found or already used")
    if inv.status != "pending":
        raise HTTPException(status_code=410, detail="Invitation has already been used or expired")
    if inv.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Invitation has expired")

    user_repo = UserRepository(User, db)
    if await user_repo.get_by_email(inv.email):
        raise HTTPException(
            status_code=409,
            detail=(
                "An account already exists for this email. Sign in, then open "
                "the invitation link again to join."
            ),
        )

    user = await user_repo.create(
        {
            "email": inv.email,
            "password_hash": hash_password(data.password),
            "full_name": data.full_name,
            "email_verified": True,
        }
    )
    await user_repo.grant_roles_by_name(
        user.user_id, ["Voter", "Event Attendee", *ORG_MEMBER_SYSTEM_ROLES]
    )

    existing_member_ids = await repo.get_member_ids(inv.org_id)
    await repo.add_member(
        org_id=inv.org_id,
        user_id=user.user_id,
        role=inv.role,
        invited_by=inv.invited_by,
    )
    await repo.accept_invitation(inv.invitation_id)
    await OrgNotificationService(db).notify(
        org_id=inv.org_id,
        member_ids=existing_member_ids,
        pref_key="invitation_accepted",
        notif_type="alert",
        title=f"{data.full_name} accepted your invitation",
        description=f"Joined as {inv.role}.",
        link="/dashboard/settings",
    )

    # Reuse the login path's token issuing so the refresh token's jti is
    # recorded and single-use rotation works the same as a normal sign-in.
    auth_service = AuthService(
        user_repo=user_repo,
        audit_repo=AuditLogRepository(AuditLog, db),
    )
    return await auth_service._issue_tokens(user)


@router.post("/invitations/accept", response_model=OrganizationMemberResponse)
async def accept_invitation(
    data: AcceptInvitationRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Accept a pending invitation. The authenticated user's email must match the invitation."""
    repo = OrganizationRepository(Organization, db)
    inv = await repo.get_invitation_by_token(data.token)
    if not inv:
        raise HTTPException(status_code=404, detail="Invitation not found or already used")
    if inv.status != "pending":
        raise HTTPException(status_code=410, detail="Invitation has already been used or expired")
    if inv.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Invitation has expired")
    if current_user.email.lower() != inv.email.lower():
        raise HTTPException(
            status_code=403,
            detail="Your account email does not match this invitation",
        )

    existing = await repo.get_member(inv.org_id, current_user.user_id)
    if existing:
        raise HTTPException(status_code=409, detail="You are already a member of this organization")

    existing_member_ids = await repo.get_member_ids(inv.org_id)
    new_member = await repo.add_member(
        org_id=inv.org_id,
        user_id=current_user.user_id,
        role=inv.role,
        invited_by=inv.invited_by,
    )

    user_repo = UserRepository(User, db)
    await user_repo.grant_roles_by_name(
        current_user.user_id, ORG_MEMBER_SYSTEM_ROLES
    )

    await repo.accept_invitation(inv.invitation_id)
    await OrgNotificationService(db).notify(
        org_id=inv.org_id,
        member_ids=existing_member_ids,
        pref_key="invitation_accepted",
        notif_type="alert",
        title=f"{current_user.full_name} accepted your invitation",
        description=f"Joined as {inv.role}.",
        link="/dashboard/settings",
    )

    return OrganizationMemberResponse(
        member_id=new_member.member_id,
        org_id=new_member.org_id,
        user_id=new_member.user_id,
        role=new_member.role,
        joined_at=new_member.joined_at,
        user_name=current_user.full_name,
        user_email=current_user.email,
    )


# --- Update member role ---


@router.put("/{org_id}/members/{member_id}", response_model=OrganizationMemberResponse)
async def update_member_role(
    request: Request,
    org_id: UUID,
    member_id: UUID,
    data: OrganizationMemberUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Change a member's role (and/or assign a custom OrgRole). Owner or
    anyone with the manage_team permission; also syncs the org-admin system
    roles (Election Administrator, Event Organizer) to match."""
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    caller = next((m for m in org.members if m.user_id == current_user.user_id), None)
    if not caller or not (caller.role == "owner" or member_has_permission(caller, MANAGE_TEAM)):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Fetch the current member record before updating so we can diff the role
    target = next((m for m in org.members if m.member_id == member_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Member not found")
    if target.role == "owner":
        raise HTTPException(status_code=400, detail="Cannot change the owner's role")

    if data.org_role_id is not None:
        assigned_role = await OrgRoleRepository(OrgRole, db).get_by_id(data.org_role_id, id_field="org_role_id")
        if not assigned_role or assigned_role.org_id != org_id:
            raise HTTPException(status_code=404, detail="Custom role not found")

    previous_role = target.role
    new_role = data.role

    updated = await repo.update_member_role(member_id, new_role, data.org_role_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Member not found")

    # Every member keeps these for as long as they belong to the org: they are
    # what lets the organizer dashboard load at all. A demotion must not blind
    # someone who is still on the team — what they lose is write access, and
    # that now follows from their membership role via
    # OrganizationRepository.can_create rather than from these platform roles.
    user_repo = UserRepository(User, db)
    await user_repo.grant_roles_by_name(updated.user_id, ORG_MEMBER_SYSTEM_ROLES)

    ip, agent = client_ip_and_agent(request)
    await OrgActivityLogRepository(OrgActivityLog, db).log_activity(
        org_id=org_id,
        category="team",
        verb="changed role of",
        target_name=target.user.full_name if target.user else "a team member",
        user_id=current_user.user_id,
        detail=f"{previous_role} → {new_role}",
        ip_address=ip,
        user_agent=agent,
    )

    return _member_to_response(updated)


# --- Remove member ---


@router.delete("/{org_id}/members/{member_id}", status_code=204)
async def remove_member(
    request: Request,
    org_id: UUID,
    member_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a member (not the owner). Revokes their org-admin system
    roles first if they held admin."""
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Owner/admin or a manage_team custom role can remove members (but not the owner themselves)
    caller = next((m for m in org.members if m.user_id == current_user.user_id), None)
    if not caller or not (caller.role in ("owner", "admin") or member_has_permission(caller, MANAGE_TEAM)):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    target = next((m for m in org.members if m.member_id == member_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Member not found")

    if target.role == "owner":
        raise HTTPException(status_code=400, detail="Cannot remove the organization owner")

    removed_user_id = target.user_id
    removed_user_name = target.user.full_name if target.user else "a team member"
    await repo.remove_member(member_id)

    ip, agent = client_ip_and_agent(request)
    await OrgActivityLogRepository(OrgActivityLog, db).log_activity(
        org_id=org_id,
        category="team",
        verb="removed",
        target_name=removed_user_name,
        user_id=current_user.user_id,
        ip_address=ip,
        user_agent=agent,
    )

    # Revoke the org-granted platform roles only once they belong to no
    # organization at all — someone on two teams must not lose access to the
    # second by leaving the first. Checked after removal so the membership
    # being deleted isn't counted.
    if not await repo.get_user_organizations(removed_user_id):
        user_repo = UserRepository(User, db)
        await user_repo.revoke_roles_by_name(removed_user_id, ORG_MEMBER_SYSTEM_ROLES)


# --- Pending invitations (list/revoke) ---


@router.get("/{org_id}/invitations", response_model=List[OrganizationInvitationResponse])
async def list_pending_invitations(
    org_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Every not-yet-accepted invitation for this org, for the Team settings
    page's own list — distinct from GET /invitations/{token}, which is the
    public single-invite lookup an invitee's link hits."""
    org_repo = OrganizationRepository(Organization, db)
    org = await org_repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    caller = next((m for m in org.members if m.user_id == current_user.user_id), None)
    if not caller or not (caller.role in ("owner", "admin") or member_has_permission(caller, MANAGE_TEAM)):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    invitations = await org_repo.get_pending_invitations_for_org(org_id)
    return [
        OrganizationInvitationResponse(
            invitation_id=inv.invitation_id,
            org_id=inv.org_id,
            org_name=org.name,
            email=inv.email,
            role=inv.role,
            status=inv.status,
            expires_at=inv.expires_at,
            created_at=inv.created_at,
        )
        for inv in invitations
    ]


@router.delete("/{org_id}/invitations/{invitation_id}", status_code=204)
async def revoke_invitation(
    org_id: UUID,
    invitation_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org_repo = OrganizationRepository(Organization, db)
    org = await org_repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    caller = next((m for m in org.members if m.user_id == current_user.user_id), None)
    if not caller or not (caller.role in ("owner", "admin") or member_has_permission(caller, MANAGE_TEAM)):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    deleted = await org_repo.delete_invitation(org_id, invitation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Invitation not found")


# --- Custom org roles (CRUD) ---


@router.post("/{org_id}/roles", response_model=OrgRoleResponse, status_code=201)
async def create_org_role(
    request: Request,
    org_id: UUID,
    data: OrgRoleCreate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org_repo = OrganizationRepository(Organization, db)
    org = await org_repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    caller = next((m for m in org.members if m.user_id == current_user.user_id), None)
    if not caller or not (caller.role in ("owner", "admin") or member_has_permission(caller, MANAGE_TEAM)):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    role_repo = OrgRoleRepository(OrgRole, db)
    if await role_repo.get_by_name(org_id, data.name):
        raise HTTPException(status_code=409, detail=f"A role named '{data.name}' already exists")

    role = await role_repo.create({"org_id": org_id, "name": data.name, "permissions": data.permissions})

    ip, agent = client_ip_and_agent(request)
    await OrgActivityLogRepository(OrgActivityLog, db).log_activity(
        org_id=org_id, category="team", verb="created a custom role", target_name=role.name,
        user_id=current_user.user_id, ip_address=ip, user_agent=agent,
    )
    return OrgRoleResponse.model_validate(role)


@router.get("/{org_id}/roles", response_model=List[OrgRoleResponse])
async def list_org_roles(
    org_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org_repo = OrganizationRepository(Organization, db)
    org = await org_repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not any(m.user_id == current_user.user_id for m in org.members):
        raise HTTPException(status_code=403, detail="Not a member of this organization")

    roles = await OrgRoleRepository(OrgRole, db).get_by_org(org_id)
    return [OrgRoleResponse.model_validate(r) for r in roles]


@router.put("/{org_id}/roles/{org_role_id}", response_model=OrgRoleResponse)
async def update_org_role(
    org_id: UUID,
    org_role_id: UUID,
    data: OrgRoleUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    org_repo = OrganizationRepository(Organization, db)
    org = await org_repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    caller = next((m for m in org.members if m.user_id == current_user.user_id), None)
    if not caller or not (caller.role in ("owner", "admin") or member_has_permission(caller, MANAGE_TEAM)):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    role_repo = OrgRoleRepository(OrgRole, db)
    role = await role_repo.get_by_id(org_role_id, id_field="org_role_id")
    if not role or role.org_id != org_id:
        raise HTTPException(status_code=404, detail="Custom role not found")

    existing = await role_repo.get_by_name(org_id, data.name)
    if existing and existing.org_role_id != org_role_id:
        raise HTTPException(status_code=409, detail=f"A role named '{data.name}' already exists")

    updated = await role_repo.update(
        org_role_id, {"name": data.name, "permissions": data.permissions}, id_field="org_role_id"
    )
    return OrgRoleResponse.model_validate(updated)


@router.delete("/{org_id}/roles/{org_role_id}", status_code=204)
async def delete_org_role(
    org_id: UUID,
    org_role_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Deletes the role; any member holding it just loses its permission
    flags going forward (org_role_id is ON DELETE SET NULL), not removed
    from the org."""
    org_repo = OrganizationRepository(Organization, db)
    org = await org_repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    caller = next((m for m in org.members if m.user_id == current_user.user_id), None)
    if not caller or not (caller.role in ("owner", "admin") or member_has_permission(caller, MANAGE_TEAM)):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    role_repo = OrgRoleRepository(OrgRole, db)
    role = await role_repo.get_by_id(org_role_id, id_field="org_role_id")
    if not role or role.org_id != org_id:
        raise HTTPException(status_code=404, detail="Custom role not found")

    await role_repo.delete(org_role_id, id_field="org_role_id")


# --- Danger zone: transfer ownership / delete (deactivate) organization ---


@router.put("/{org_id}/transfer-ownership", response_model=OrganizationResponse)
async def transfer_ownership(
    request: Request,
    org_id: UUID,
    data: TransferOwnershipRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Hand full ownership to another member. Owner-only — unlike other
    settings actions, a manage_settings/manage_team custom role is not
    enough, since this also changes who can delete the organization."""
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org or not org.is_active:
        raise HTTPException(status_code=404, detail="Organization not found")

    caller = next((m for m in org.members if m.user_id == current_user.user_id), None)
    if not caller or caller.role != "owner":
        raise HTTPException(status_code=403, detail="Only the organization owner can transfer ownership")

    target = next((m for m in org.members if m.member_id == data.new_owner_member_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Member not found")
    if target.member_id == caller.member_id:
        raise HTTPException(status_code=400, detail="You already own this organization")

    org = await repo.transfer_ownership(org, caller, target)

    ip, agent = client_ip_and_agent(request)
    await OrgActivityLogRepository(OrgActivityLog, db).log_activity(
        org_id=org_id,
        category="settings",
        verb="transferred ownership to",
        target_name=target.user.full_name if target.user else "a team member",
        user_id=current_user.user_id,
        ip_address=ip,
        user_agent=agent,
    )

    org = await repo.get_with_members(org_id)
    return _org_to_response(org, viewer=current_user)


@router.delete("/{org_id}", status_code=204)
async def delete_organization(
    request: Request,
    org_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Deactivates the organization rather than destroying its rows — the
    elections/events/votes/tickets/payouts underneath it are real financial
    and voting records that must stay recoverable and auditable. Blocked
    while any election/event is live or any payout is still pending, since
    either would strand real money or an in-progress vote."""
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org or not org.is_active:
        raise HTTPException(status_code=404, detail="Organization not found")

    caller = next((m for m in org.members if m.user_id == current_user.user_id), None)
    if not caller or caller.role != "owner":
        raise HTTPException(status_code=403, detail="Only the organization owner can delete this organization")

    election_repo = ElectionRepository(Election, db)
    event_repo = EventRepository(Event, db)
    elections = await election_repo.get_by_org(org_id)
    events = await event_repo.get_by_org(org_id)

    if any(e.status == "active" for e in elections) or any(e.status == "active" for e in events):
        raise HTTPException(
            status_code=400,
            detail="Cannot delete an organization with a live election or event — close it first",
        )

    payout_repo = PayoutRequestRepository(PayoutRequest, db)
    _, _, pending_events, pending_elections = await payout_repo.get_totals_by_parents(
        event_ids=[e.event_id for e in events],
        election_ids=[e.election_id for e in elections],
    )
    if pending_events or pending_elections:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete an organization with a pending payout request — resolve it first",
        )

    await repo.deactivate(org_id)

    ip, agent = client_ip_and_agent(request)
    await OrgActivityLogRepository(OrgActivityLog, db).log_activity(
        org_id=org_id,
        category="settings",
        verb="deleted",
        target_name=org.name,
        user_id=current_user.user_id,
        ip_address=ip,
        user_agent=agent,
    )


# --- KYC document upload ---

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB
ALLOWED_IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}


@router.post("/{org_id}/logo/upload", response_model=OrganizationResponse)
async def upload_organization_logo(
    org_id: UUID,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload an organization logo and persist its download URL."""
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    if org.owner_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="Only the organization owner can upload a logo")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Allowed: JPG, PNG, WEBP",
        )

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="Image must be under 100 MB")

    logo_url = await file_storage_service.upload(
        content=content,
        filename=file.filename or f"logo{ext}",
        content_type=file.content_type,
    )

    await repo.update(org_id, {"logo_url": logo_url}, id_field="org_id")
    org = await repo.get_with_members(org_id)
    return _org_to_response(org, viewer=current_user)


@router.post("/{org_id}/upload-documents", response_model=OrganizationResponse)
async def upload_kyc_documents(
    org_id: UUID,
    front: UploadFile = File(...),
    back: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload front and back KYC identity documents for an organization."""
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    if org.owner_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="Only the organization owner can upload documents")

    # Validate file types and sizes, then upload to the file-storage service
    async def _upload(upload: UploadFile, label: str) -> str:
        ext = Path(upload.filename or "").suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"File '{upload.filename}' has unsupported type. Allowed: JPG, PNG, PDF",
            )
        content = await upload.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"File '{upload.filename}' exceeds 100 MB limit")
        # upload_private (not upload): identity documents must resolve to the
        # authenticated /files/private route, never the public image one.
        return await file_storage_service.upload_private(
            content=content,
            filename=upload.filename or f"{label}{ext}",
            content_type=upload.content_type,
        )

    front_path = await _upload(front, "front")
    back_path = await _upload(back, "back")

    await repo.update(
        org_id,
        {"kyc_document_front": front_path, "kyc_document_back": back_path},
        id_field="org_id",
    )

    org = await repo.get_with_members(org_id)
    return _org_to_response(org, viewer=current_user)
