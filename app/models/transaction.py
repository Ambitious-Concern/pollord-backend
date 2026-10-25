import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Transaction(Base):
    """A Paystack payment for one or more paid votes — the vote-side
    counterpart of TicketTransaction."""

    __tablename__ = "transactions"

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    reference: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    # Plain (no FK) columns, matching this table's existing convention. Exactly one
    # of election_id/event_id is set depending on which parent the category belongs to.
    election_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    event_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    voter_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # JSON array of candidate UUID strings: ["uuid1", "uuid2"]
    candidate_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)   # in kobo
    currency: Mapped[str] = mapped_column(String(10), default="GHS")
    # pending | success | failed | abandoned
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # Plain (no FK), matching this table's existing convention. Set at
    # initiation when a promo code discounted this payment.
    promo_code_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    discount_pesewas: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Decided at initiation and trusted as-is at verify time, rather than
    # re-derived from amount // vote_price — that division would silently
    # undercount once a promo code changes the per-vote price.
    vote_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Snapshotted from the parent election/event's fee_payer at initiation,
    # not re-read live — an organizer flipping the toggle after some sales
    # already happened shouldn't retroactively change what past transactions
    # are reported as. See app/core/financial_breakdown.py.
    fee_payer: Mapped[str] = mapped_column(String(20), default="organizer", nullable=False)
    paystack_response: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
