import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User

CATEGORIES = (
    "election", "event", "voting", "ticketing",
    "payout", "front-desk", "team", "settings",
)


class OrgActivityLog(Base):
    """A team-facing activity feed, scoped to one organization — distinct
    from AuditLog, which is the platform-wide log the System Administrator
    console reads. user_id is nullable: null means a system-generated entry
    (e.g. an automated payout completion) rather than a teammate's action."""

    __tablename__ = "org_activity_logs"
    __table_args__ = (
        Index("idx_org_activity_org_created", "org_id", "created_at"),
    )

    log_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.org_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True
    )
    category: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    verb: Mapped[str] = mapped_column(String(100), nullable=False)
    # Denormalized snapshot, not a live join — the entry stays readable even
    # after the target election/event/member is renamed or deleted.
    target_name: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped[Optional["User"]] = relationship(lazy="select")
