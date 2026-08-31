import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    and_,
    func,
    or_,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.event import Event, TicketType
    from app.models.user import User


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(
            "user_id IS NOT NULL OR guest_email IS NOT NULL",
            name="ck_tickets_owner_present",
        ),
    )

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
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True, index=True
    )
    # Guest (unauthenticated) purchaser identity — populated instead of user_id
    # when a ticket is bought without an account.
    guest_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    guest_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    guest_phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
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
    user: Mapped[Optional["User"]] = relationship(
        back_populates="tickets", foreign_keys=[user_id], lazy="select"
    )
    purchase: Mapped["TicketPurchase"] = relationship(
        back_populates="tickets", lazy="select"
    )


class TicketPurchase(Base):
    __tablename__ = "ticket_purchases"
    __table_args__ = (
        CheckConstraint(
            "user_id IS NOT NULL OR guest_email IS NOT NULL",
            name="ck_ticket_purchases_owner_present",
        ),
    )

    purchase_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True
    )
    guest_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    guest_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    guest_phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
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
    # Outcome of the last ticket-confirmation email attempt: "sent" or
    # "failed". NULL means unknown — either the purchase predates this
    # tracking or no attempt was ever made (e.g. no address to send to).
    # Without this an admin has no way to find the buyers whose ticket
    # email silently failed, since send_email only logs its failures.
    confirmation_email_status: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True, index=True
    )
    confirmation_email_attempted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The address the last attempt went to — may differ from the buyer's
    # own address when an admin resends to a corrected one.
    confirmation_email_to: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )

    # Relationships
    user: Mapped[Optional["User"]] = relationship(
        back_populates="purchases", lazy="select"
    )
    event: Mapped["Event"] = relationship(
        back_populates="purchases", lazy="select"
    )
    tickets: Mapped[list["Ticket"]] = relationship(
        back_populates="purchase", lazy="selectin"
    )


# A ticket belongs to a user either directly (user_id, bought while signed in)
# or indirectly — guest checkout records only guest_email, so a ticket bought
# with the address on someone's account is still theirs.
def owned_by(user_id: uuid.UUID, email: Optional[str] = None):
    """SQL predicate for the tickets a user owns."""
    owned = Ticket.user_id == user_id
    if email:
        owned = or_(
            owned,
            and_(
                Ticket.user_id.is_(None),
                func.lower(Ticket.guest_email) == email.lower(),
            ),
        )
    return owned


def is_owned_by(ticket: "Ticket", user: "User") -> bool:
    """Object-level counterpart of owned_by(), for authorizing a loaded ticket."""
    if ticket.user_id is not None:
        return ticket.user_id == user.user_id
    return bool(
        ticket.guest_email
        and user.email
        and ticket.guest_email.lower() == user.email.lower()
    )
