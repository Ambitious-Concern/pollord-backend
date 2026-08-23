import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    Boolean, Date, DateTime, Integer, Numeric, String, Text, Time, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import ForeignKey

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.ticket import Ticket, TicketPurchase
    from app.models.user import User


class Event(TimestampMixin, Base):
    __tablename__ = "events"

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    event_time: Mapped[time] = mapped_column(Time, nullable=False)
    location: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    capacity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    banner_image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    # Whether buyers see how many tickets are left. Sold-out state is always
    # shown regardless — that's something a buyer needs, not a sales figure.
    # Defaults true so existing events keep behaving as they do today.
    show_ticket_counts: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    # Whether ticket check-in is open. Switching this off kills every
    # outstanding check-in link immediately — that's the point, so a leaked
    # link can be revoked mid-event. Defaults true so existing events keep
    # scanning as they do today.
    scan_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )

    # Relationships
    creator: Mapped["User"] = relationship(
        back_populates="created_events", lazy="select"
    )
    ticket_types: Mapped[List["TicketType"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", lazy="selectin"
    )
    tickets: Mapped[List["Ticket"]] = relationship(
        back_populates="event", lazy="select"
    )
    purchases: Mapped[List["TicketPurchase"]] = relationship(
        back_populates="event", lazy="select"
    )


class TicketType(TimestampMixin, Base):
    __tablename__ = "ticket_types"

    ticket_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.event_id", ondelete="CASCADE"),
        nullable=False,
    )
    type_name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    quantity_available: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_sold: Mapped[int] = mapped_column(Integer, default=0)
    sales_start_datetime: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sales_end_datetime: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    max_per_user: Mapped[int] = mapped_column(Integer, default=5)
    status: Mapped[str] = mapped_column(String(20), default="active")

    # Relationships
    event: Mapped["Event"] = relationship(
        back_populates="ticket_types", lazy="select"
    )
    tickets: Mapped[List["Ticket"]] = relationship(
        back_populates="ticket_type", lazy="select"
    )
