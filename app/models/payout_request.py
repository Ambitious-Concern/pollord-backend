import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.event import Event
    from app.models.user import User


class PayoutRequest(TimestampMixin, Base):
    """An organizer's request to be paid the revenue collected for an event.
    No money moves automatically — an admin reviews this manually and pays the
    organizer outside the app, then marks it paid here."""

    __tablename__ = "payout_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'paid', 'rejected')",
            name="ck_payout_requests_status",
        ),
    )

    payout_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.event_id"), nullable=False, index=True
    )
    organizer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    admin_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True
    )

    event: Mapped["Event"] = relationship(foreign_keys=[event_id], lazy="select")
    organizer: Mapped["User"] = relationship(foreign_keys=[organizer_id], lazy="select")
    reviewer: Mapped[Optional["User"]] = relationship(foreign_keys=[reviewed_by], lazy="select")
