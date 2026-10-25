import secrets
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.event import Event
    from app.models.user import User


class EventInvitation(Base):
    """A token-based RSVP invite for an event — same shape as
    OrganizationInvitation, but no account is required to respond since an
    RSVP (unlike org membership) doesn't need to bind to a user identity."""

    __tablename__ = "event_invitations"

    invitation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.event_id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    token: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, default=lambda: secrets.token_urlsafe(48)
    )
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | accepted | declined | expired
    invited_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    event: Mapped["Event"] = relationship(foreign_keys=[event_id], lazy="selectin")
    inviter: Mapped[Optional["User"]] = relationship(foreign_keys=[invited_by], lazy="select")
