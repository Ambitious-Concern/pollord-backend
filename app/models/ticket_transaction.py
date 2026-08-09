import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TicketTransaction(Base):
    __tablename__ = "ticket_transactions"
    __table_args__ = (
        CheckConstraint(
            "user_id IS NOT NULL OR guest_email IS NOT NULL",
            name="ck_ticket_transactions_owner_present",
        ),
    )

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    reference: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    guest_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    guest_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    guest_phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    # [{"ticket_type_id": "...", "quantity": 2}, ...]
    items: Mapped[list] = mapped_column(JSONB, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="GHS")
    # pending | success | failed | needs_refund
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    paystack_response: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    purchase_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
