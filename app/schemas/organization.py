from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator


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
    website: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    industry: Optional[str] = None


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

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        allowed = {"admin", "editor", "member"}
        if v not in allowed:
            raise ValueError(f"Role must be one of: {allowed}")
        return v


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


class OrganizationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    kyc_document_front: Optional[str] = None
    kyc_document_back: Optional[str] = None
    owner_id: UUID
    created_at: datetime
    updated_at: datetime
    members: List[OrganizationMemberResponse] = []


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
