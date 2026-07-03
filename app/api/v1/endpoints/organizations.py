from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_current_active_user
from app.db.base import get_db
from app.models.organization import Organization, OrganizationMember
from app.models.user import User
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.user_repository import UserRepository
from app.schemas.organization import (
    AcceptInvitationRequest,
    OrganizationCreate,
    OrganizationInvitationResponse,
    OrganizationMemberAdd,
    OrganizationMemberResponse,
    OrganizationMemberUpdate,
    OrganizationResponse,
    OrganizationUpdate,
)
from app.services import email_service
from app.services.file_storage_service import file_storage_service

# System roles granted to org owners and admins so they can create elections/events
ORG_ADMIN_SYSTEM_ROLES = ["Election Administrator", "Event Organizer"]


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


class OrganizationPublicResponse(BaseModel):
    """Subset of org data safe to expose without authentication."""
    org_id: UUID
    name: str
    description: Optional[str] = None
    logo_url: Optional[str] = None
    website: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    industry: Optional[str] = None
    is_verified: bool
    owner_id: UUID
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
    )


def _org_to_response(org: Organization) -> OrganizationResponse:
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
    await user_repo.grant_roles_by_name(current_user.user_id, ORG_ADMIN_SYSTEM_ROLES)

    org = await repo.get_with_members(org.org_id)
    return _org_to_response(org)


# --- List user's organizations ---


@router.get("/my", response_model=List[OrganizationResponse])
async def list_my_organizations(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """List all organizations the user owns or is a member of."""
    repo = OrganizationRepository(Organization, db)
    orgs = await repo.get_user_organizations(current_user.user_id)
    return [_org_to_response(o) for o in orgs]


# --- Public org info by owner user_id (no auth required) ---
# The public share URL uses /:userId/explore where userId is the owner's user_id.


@router.get("/owner/{user_id}", response_model=OrganizationPublicResponse)
async def get_org_by_owner(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Return the organization owned by a specific user. No auth required."""
    repo = OrganizationRepository(Organization, db)
    orgs = await repo.get_by_owner(user_id)
    if not orgs:
        raise HTTPException(status_code=404, detail="Organization not found for this user")
    org = orgs[0]

    return OrganizationPublicResponse(
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
        owner_id=org.owner_id,
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
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    return OrganizationPublicResponse(
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
        owner_id=org.owner_id,
        created_at=org.created_at,
        member_count=len(org.members or []),
    )


# --- Get single organization ---


@router.get("/{org_id}", response_model=OrganizationResponse)
async def get_organization(
    org_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Check membership
    is_member = any(m.user_id == current_user.user_id for m in org.members)
    if not is_member:
        raise HTTPException(status_code=403, detail="Not a member of this organization")

    return _org_to_response(org)


# --- Update organization ---


@router.put("/{org_id}", response_model=OrganizationResponse)
async def update_organization(
    org_id: UUID,
    data: OrganizationUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Only owner or admin can update
    member = next((m for m in org.members if m.user_id == current_user.user_id), None)
    if not member or member.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    update_data = data.model_dump(exclude_unset=True)
    # Convert a base64 logo data URI to a stored URL before persisting.
    if "logo_url" in update_data:
        update_data["logo_url"] = await file_storage_service.resolve(
            update_data["logo_url"], filename_hint="logo"
        )

    updated = await repo.update(org_id, update_data, id_field="org_id")
    org = await repo.get_with_members(org_id)
    return _org_to_response(org)


# --- Add member ---


@router.post("/{org_id}/members", response_model=OrganizationMemberResponse, status_code=201)
async def add_member(
    org_id: UUID,
    data: OrganizationMemberAdd,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Only owner or admin can add members
    member = next((m for m in org.members if m.user_id == current_user.user_id), None)
    if not member or member.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Check if already a member
    existing = await repo.get_member(org_id, data.user_id)
    if existing:
        raise HTTPException(status_code=409, detail="User is already a member")

    new_member = await repo.add_member(
        org_id=org_id,
        user_id=data.user_id,
        role=data.role,
        invited_by=current_user.user_id,
    )

    # Grant system roles to admins so they can create elections/events
    if data.role == "admin":
        user_repo = UserRepository(User, db)
        await user_repo.grant_roles_by_name(data.user_id, ORG_ADMIN_SYSTEM_ROLES)

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
    if not caller or caller.role not in ("owner", "admin"):
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

        new_member = await repo.add_member(
            org_id=org_id,
            user_id=target.user_id,
            role=data.role,
            invited_by=current_user.user_id,
        )
        if data.role == "admin":
            await user_repo.grant_roles_by_name(target.user_id, ORG_ADMIN_SYSTEM_ROLES)

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

    new_member = await repo.add_member(
        org_id=inv.org_id,
        user_id=current_user.user_id,
        role=inv.role,
        invited_by=inv.invited_by,
    )

    if inv.role == "admin":
        user_repo = UserRepository(User, db)
        await user_repo.grant_roles_by_name(current_user.user_id, ORG_ADMIN_SYSTEM_ROLES)

    await repo.accept_invitation(inv.invitation_id)

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
    org_id: UUID,
    member_id: UUID,
    data: OrganizationMemberUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Only owner can change roles
    caller = next((m for m in org.members if m.user_id == current_user.user_id), None)
    if not caller or caller.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can change roles")

    # Fetch the current member record before updating so we can diff the role
    target = next((m for m in org.members if m.member_id == member_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Member not found")
    if target.role == "owner":
        raise HTTPException(status_code=400, detail="Cannot change the owner's role")

    previous_role = target.role
    new_role = data.role

    updated = await repo.update_member_role(member_id, new_role)
    if not updated:
        raise HTTPException(status_code=404, detail="Member not found")

    # Sync system roles based on role change
    user_repo = UserRepository(User, db)
    if new_role == "admin" and previous_role != "admin":
        # Promoted to admin — grant Election Administrator + Event Organizer
        await user_repo.grant_roles_by_name(updated.user_id, ORG_ADMIN_SYSTEM_ROLES)
    elif previous_role == "admin" and new_role != "admin":
        # Demoted from admin — revoke those system roles so they lose create access
        await user_repo.revoke_roles_by_name(updated.user_id, ORG_ADMIN_SYSTEM_ROLES)

    return OrganizationMemberResponse(
        member_id=updated.member_id,
        org_id=updated.org_id,
        user_id=updated.user_id,
        role=updated.role,
        joined_at=updated.joined_at,
    )


# --- Remove member ---


@router.delete("/{org_id}/members/{member_id}", status_code=204)
async def remove_member(
    org_id: UUID,
    member_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    repo = OrganizationRepository(Organization, db)
    org = await repo.get_with_members(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Owner or admin can remove members (but not the owner themselves)
    caller = next((m for m in org.members if m.user_id == current_user.user_id), None)
    if not caller or caller.role not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    target = next((m for m in org.members if m.member_id == member_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Member not found")

    if target.role == "owner":
        raise HTTPException(status_code=400, detail="Cannot remove the organization owner")

    # If removing an admin, revoke their org-granted system roles
    if target.role == "admin":
        user_repo = UserRepository(User, db)
        await user_repo.revoke_roles_by_name(target.user_id, ORG_ADMIN_SYSTEM_ROLES)

    await repo.remove_member(member_id)


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
    return _org_to_response(org)


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
        return await file_storage_service.upload(
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
    return _org_to_response(org)
