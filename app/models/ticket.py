import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.event import Event, TicketType
    from app.models.user import User


class Ticket(Base):
    __tablename__ = "tickets"

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    ticket_code: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False, index=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.event_id"), nullable=False, index=True
    )
    ticket_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ticket_types.ticket_type_id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False, index=True
    )
    purchase_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ticket_purchases.purchase_id"), nullable=False
    )
    qr_code_data: Mapped[str] = mapped_column(Text, nullable=False)
    purchase_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    ticket_status: Mapped[str] = mapped_column(String(20), default="valid")
    used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scanned_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True
    )

    # Relationships
    event: Mapped["Event"] = relationship(
        back_populates="tickets", lazy="select"
    )
    ticket_type: Mapped["TicketType"] = relationship(
        back_populates="tickets", lazy="selectin"
    )
    user: Mapped["User"] = relationship(
        back_populates="tickets", foreign_keys=[user_id], lazy="select"
    )
    purchase: Mapped["TicketPurchase"] = relationship(
        back_populates="tickets", lazy="select"
    )


class TicketPurchase(Base):
    __tablename__ = "ticket_purchases"

    purchase_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.event_id"), nullable=False
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    payment_status: Mapped[str] = mapped_column(String(20), default="pending")
    payment_method: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    payment_reference: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    purchased_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    user: Mapped["User"] = relationship(
        back_populates="purchases", lazy="select"
    )
    event: Mapped["Event"] = relationship(
        back_populates="purchases", lazy="select"
    )
    tickets: Mapped[list["Ticket"]] = relationship(
        back_populates="purchase", lazy="selectin"
    )
