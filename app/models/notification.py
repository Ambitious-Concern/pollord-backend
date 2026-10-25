import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User

# Mirrors the frontend's NotificationType union exactly (see
# pollord-frontend's notifications-data.ts) so no remapping is needed
# between what the backend stores and what the bell/panel render.
NOTIFICATION_TYPES = ("vote", "ticket", "payout", "nomination", "verification", "alert")


class Notification(Base):
    """One user's dashboard notification feed entry. Scoped by exact
    user_id match everywhere it's queried — same "not a tenant table"
    treatment as refresh_tokens in the RLS plan, since a notification
    belongs to one person, not an organization's shared data."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("idx_notifications_user_created", "user_id", "created_at"),
    )

    notification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.org_id", ondelete="CASCADE"), nullable=True
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    link: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(lazy="select")
