import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.audit_log import AuditLog
    from app.models.election import Election, EligibleVoter
    from app.models.event import Event
    from app.models.ticket import Ticket, TicketPurchase
    from app.models.vote import VoteReceipt


class User(TimestampMixin, Base):
    __tablename__ = "users"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    account_status: Mapped[str] = mapped_column(
        String(20), default="active", index=True
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    otp_code: Mapped[Optional[str]] = mapped_column(String(6), nullable=True)
    otp_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    user_roles: Mapped[List["UserRole"]] = relationship(
        back_populates="user",
        foreign_keys="UserRole.user_id",
        lazy="selectin",
    )
    created_elections: Mapped[List["Election"]] = relationship(
        back_populates="creator", lazy="select"
    )
    created_events: Mapped[List["Event"]] = relationship(
        back_populates="creator", lazy="select"
    )
    eligible_voters: Mapped[List["EligibleVoter"]] = relationship(
        back_populates="user", lazy="select"
    )
    vote_receipts: Mapped[List["VoteReceipt"]] = relationship(
        back_populates="user", lazy="select"
    )
    tickets: Mapped[List["Ticket"]] = relationship(
        back_populates="user", foreign_keys="Ticket.user_id", lazy="select"
    )
    purchases: Mapped[List["TicketPurchase"]] = relationship(
        back_populates="user", lazy="select"
    )
    audit_logs: Mapped[List["AuditLog"]] = relationship(
        back_populates="user", lazy="select"
    )


class Role(TimestampMixin, Base):
    __tablename__ = "roles"

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    role_name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    permissions: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # Relationships
    user_roles: Mapped[List["UserRole"]] = relationship(
        back_populates="role", lazy="select"
    )


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("roles.role_id", ondelete="CASCADE"),
        primary_key=True,
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    assigned_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True
    )

    # Relationships
    user: Mapped["User"] = relationship(
        back_populates="user_roles", foreign_keys=[user_id]
    )
    role: Mapped["Role"] = relationship(back_populates="user_roles")
