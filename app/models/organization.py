import secrets
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, Boolean, JSON, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class Organization(TimestampMixin, Base):
    """A KYC-verified account that owns elections/events; created_by/owner_id
    is the user who ran KYC, with other users joining as OrganizationMembers."""

    __tablename__ = "organizations"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    logo_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Premium+ perk — see require_election_plan/require_event_plan for the gate.
    brand_primary_color: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)
    website: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    industry: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    kyc_document_front: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    kyc_document_back: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    # Soft-delete: "Delete organization" deactivates rather than destroys
    # rows, since elections/events/votes/tickets/payouts underneath it are
    # real financial/voting records that must stay recoverable and auditable.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    deactivated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    owner: Mapped["User"] = relationship(
        foreign_keys=[owner_id], lazy="select"
    )
    members: Mapped[List["OrganizationMember"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan", lazy="selectin"
    )


class OrganizationMember(Base):
    """A user's membership in an Organization, with a role (owner/admin/
    editor/member) that controls what they can manage."""

    __tablename__ = "organization_members"

    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.org_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(
        String(30), default="member"
    )  # owner | admin | editor | member
    # Optional, additive — grants whatever specific permission flags the
    # assigned OrgRole lists, on top of (never instead of) the coarse role
    # above. See app/core/org_permissions.py for how the two combine.
    org_role_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("org_roles.org_role_id", ondelete="SET NULL"), nullable=True
    )
    invited_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(
        back_populates="members", lazy="select"
    )
    user: Mapped["User"] = relationship(
        foreign_keys=[user_id], lazy="selectin"
    )
    org_role: Mapped[Optional["OrgRole"]] = relationship(
        foreign_keys=[org_role_id], lazy="selectin"
    )


class OrgRole(Base):
    """An organization-defined custom role: a name plus a set of permission
    flags (see app/core/org_permissions.py for the fixed, valid flag list).

    Deliberately separate from the platform-wide `Role`/`UserRole` system
    (app/models/user.py) — that table is global and checked by hardcoded
    name across ~40 endpoint files; this one is scoped to a single
    organization's own resources and checked via member_has_permission()."""

    __tablename__ = "org_roles"

    org_role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.org_id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    permissions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(foreign_keys=[org_id], lazy="select")


class OrganizationInvitation(Base):
    """Pending invitation for a user (who may not yet have an account)."""
    __tablename__ = "organization_invitations"

    invitation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.org_id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(30), default="member")
    token: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, default=lambda: secrets.token_urlsafe(48)
    )
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | accepted | expired
    invited_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(foreign_keys=[org_id], lazy="selectin")
    inviter: Mapped[Optional["User"]] = relationship(foreign_keys=[invited_by], lazy="select")
