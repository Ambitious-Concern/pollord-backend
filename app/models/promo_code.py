import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, String, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.election import Election
    from app.models.event import Event
    from app.models.organization import Organization
    from app.models.user import User

DISCOUNT_TYPES = ("percentage", "fixed_amount")


class PromoCode(TimestampMixin, Base):
    """A buyer-entered discount code for ticket purchases and paid votes.
    Scope (see PromoCodeScope) is a list of elections/events it applies to —
    an empty list means it applies to everything the owning org runs.
    usesCount is deliberately not stored here; it's derived from
    PromoCodeRedemption so it can never drift from what was actually
    redeemed (same reasoning as vote_count/has_voted elsewhere)."""

    __tablename__ = "promo_codes"

    promo_code_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # See Election.org_id for why this is nullable — same personal-vs-org
    # rule applies here.
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.org_id"), nullable=True, index=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(30), unique=True, index=True, nullable=False)
    discount_type: Mapped[str] = mapped_column(String(20), nullable=False)
    discount_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    usage_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    per_person_limit: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_until: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # "Expired" (valid_until in the past) is derived at read time, not stored —
    # this only tracks the organizer's own Active/Inactive toggle.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    scopes: Mapped[List["PromoCodeScope"]] = relationship(
        back_populates="promo_code", cascade="all, delete-orphan", lazy="selectin"
    )
    creator: Mapped["User"] = relationship(lazy="select")
    organization: Mapped[Optional["Organization"]] = relationship(lazy="select")


class PromoCodeScope(Base):
    """One (election XOR event) a promo code applies to. No rows at all for
    a code means "applies to everything" — see PromoCode's docstring."""

    __tablename__ = "promo_code_scopes"
    __table_args__ = (
        CheckConstraint(
            "(election_id IS NOT NULL) != (event_id IS NOT NULL)",
            name="ck_promo_scope_exactly_one_parent",
        ),
    )

    scope_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    promo_code_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("promo_codes.promo_code_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    election_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("elections.election_id", ondelete="CASCADE"), nullable=True
    )
    event_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.event_id", ondelete="CASCADE"), nullable=True
    )

    promo_code: Mapped["PromoCode"] = relationship(back_populates="scopes")
    election: Mapped[Optional["Election"]] = relationship(lazy="select")
    event: Mapped[Optional["Event"]] = relationship(lazy="select")


class PromoCodeRedemption(Base):
    """One successful use of a promo code — an append-only ledger, never
    updated after creation. usage_limit/per_person_limit are enforced by
    counting rows here rather than a mutable counter, so a failed/abandoned
    payment can never leave the count wrong."""

    __tablename__ = "promo_code_redemptions"

    redemption_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    promo_code_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("promo_codes.promo_code_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # "ticket" or "vote" — which checkout flow redeemed it.
    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True
    )
    # Always populated even for a signed-in user — the one identity dimension
    # both guest checkout and anonymous voting always have, so per-person
    # limits work identically for every buyer type.
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    promo_code: Mapped["PromoCode"] = relationship(lazy="select")
