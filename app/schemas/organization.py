import re
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.schemas.user import _validate_password_strength


class OrganizationCreate(BaseModel):
    """Created during KYC — the user's organization."""
    name: str
    description: Optional[str] = None
    logo_url: Optional[str] = None
    website: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    industry: Optional[str] = None


class OrganizationUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    logo_url: Optional[str] = None
    # Premium+ only — see update_organization's plan check.
    brand_primary_color: Optional[str] = None
    website: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    industry: Optional[str] = None

    @field_validator("brand_primary_color")
    @classmethod
    def validate_brand_primary_color(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            raise ValueError("brand_primary_color must be a 6-digit hex color, e.g. #2563EB")
        return v


class OrganizationMemberAdd(BaseModel):
    user_id: UUID
    role: str = "member"

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        allowed = {"admin", "editor", "member"}
        if v not in allowed:
            raise ValueError(f"Role must be one of: {allowed}")
        return v


class OrganizationMemberUpdate(BaseModel):
    role: str
    # Optional custom OrgRole to assign alongside the coarse role above.
    # Pass null explicitly to unassign; omit to leave whatever's already set.
    org_role_id: Optional[UUID] = None

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        allowed = {"admin", "editor", "member"}
        if v not in allowed:
            raise ValueError(f"Role must be one of: {allowed}")
        return v


class OrgRoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    permissions: List[str] = Field(default_factory=list)

    @field_validator("permissions")
    @classmethod
    def validate_permissions(cls, v: List[str]) -> List[str]:
        from app.core.org_permissions import ORG_PERMISSIONS

        invalid = set(v) - set(ORG_PERMISSIONS)
        if invalid:
            raise ValueError(f"Unknown permission(s): {sorted(invalid)}. Valid: {ORG_PERMISSIONS}")
        return v


class OrgRoleUpdate(OrgRoleCreate):
    pass


class OrgRoleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    org_role_id: UUID
    org_id: UUID
    name: str
    permissions: List[str]
    created_at: datetime


class OrganizationMemberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    member_id: UUID
    org_id: UUID
    user_id: UUID
    role: str
    joined_at: datetime
    # Populated from the user relationship
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    org_role: Optional[OrgRoleResponse] = None


class OrganizationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    kyc_document_front: Optional[str] = None
    kyc_document_back: Optional[str] = None
    owner_id: UUID
    is_active: bool
    deactivated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    members: List[OrganizationMemberResponse] = []


class TransferOwnershipRequest(BaseModel):
    new_owner_member_id: UUID


class OrganizationInvitationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    invitation_id: UUID
    org_id: UUID
    org_name: str
    email: str
    role: str
    status: str
    expires_at: datetime
    created_at: datetime


class AcceptInvitationRequest(BaseModel):
    token: str


class AcceptInvitationSignupRequest(BaseModel):
    """Joining from an invitation email: name and password, nothing else.

    The email is taken from the invitation, so the invitee never retypes an
    address we already know and have proven they control.
    """

    token: str
    full_name: str = Field(min_length=1, max_length=255)
    password: str

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return _validate_password_strength(v)
