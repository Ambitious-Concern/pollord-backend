import random
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.vote import Vote, VoteReceipt
    from app.models.event import Event


class Election(TimestampMixin, Base):
    __tablename__ = "elections"

    election_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[Optional[str]] = mapped_column(String(255), unique=True, index=True, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    start_datetime: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    end_datetime: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )

    # --- New fields ---
    banner_image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    visibility: Mapped[str] = mapped_column(String(20), default="public")  # public | private
    access_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    allow_result_viewing: Mapped[str] = mapped_column(
        String(20), default="after_end"
    )  # live | after_end | admin_only
    require_verification: Mapped[bool] = mapped_column(Boolean, default=False)
    anonymous_results: Mapped[bool] = mapped_column(Boolean, default=True)
    show_candidate_count: Mapped[bool] = mapped_column(Boolean, default=False)
    randomize_candidate_order: Mapped[bool] = mapped_column(Boolean, default=False)
    enable_notifications: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_revoting: Mapped[bool] = mapped_column(Boolean, default=False)
    vote_price: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    settings_extra: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Venue/location — optional, since not every election is tied to a physical
    # place. Named `tag` (not `category`) to avoid colliding with the unrelated
    # `Category` model, the same footgun already flagged on Event.category.
    venue: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tag: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Relationships
    creator: Mapped["User"] = relationship(
        back_populates="created_elections", lazy="select"
    )
    categories: Mapped[List["Category"]] = relationship(
        back_populates="election", cascade="all, delete-orphan", lazy="selectin"
    )
    eligible_voters: Mapped[List["EligibleVoter"]] = relationship(
        back_populates="election", cascade="all, delete-orphan", lazy="select"
    )
    votes: Mapped[List["Vote"]] = relationship(
        back_populates="election", lazy="select"
    )
    vote_receipts: Mapped[List["VoteReceipt"]] = relationship(
        back_populates="election", lazy="select"
    )


class Category(TimestampMixin, Base):
    """A position/prize within an Election or Event — e.g. "President", "Best Dressed".

    Attaches to exactly one parent (Election OR Event, never both/neither — see
    ck_category_exactly_one_parent). NOT the same thing as Event.category (a plain
    string tag like "Conference"/"Concert") — that column is unrelated and untouched.
    """
    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint(
            "(election_id IS NOT NULL) != (event_id IS NOT NULL)",
            name="ck_category_exactly_one_parent",
        ),
    )

    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    election_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("elections.election_id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    event_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.event_id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    election_type: Mapped[str] = mapped_column(String(50), nullable=False)
    allow_abstain: Mapped[bool] = mapped_column(Boolean, default=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    # Relationships
    election: Mapped[Optional["Election"]] = relationship(
        back_populates="categories", lazy="select"
    )
    event: Mapped[Optional["Event"]] = relationship(
        back_populates="categories", lazy="select"
    )
    candidates: Mapped[List["Candidate"]] = relationship(
        back_populates="category", cascade="all, delete-orphan", lazy="selectin"
    )


class Candidate(TimestampMixin, Base):
    __tablename__ = "candidates"

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.category_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Denormalized parent references, kept in sync with category.election_id/event_id
    # by the service layer — load-bearing for CandidateAccessOTP, short-code lookups,
    # and other election-scoped queries that predate categories.
    election_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("elections.election_id", ondelete="CASCADE"),
        nullable=True,
    )
    event_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.event_id", ondelete="CASCADE"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    short_code: Mapped[Optional[str]] = mapped_column(String(6), nullable=True, index=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    # Relationships
    category: Mapped["Category"] = relationship(
        back_populates="candidates", lazy="select"
    )


class CandidateAccessOTP(Base):
    """One-time passcode for a candidate to view their own election results."""
    __tablename__ = "candidate_access_otps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    election_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("elections.election_id", ondelete="CASCADE"),
        nullable=False,
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("candidates.candidate_id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    otp_code: Mapped[str] = mapped_column(String(6), nullable=False,
                                          default=lambda: f"{random.randint(0, 999999):06d}")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class VoterOTP(Base):
    """One-time passcode verifying an anonymous voter's email before a free
    public vote is cast — a stronger identity check than the plain IP+user-agent
    hash, without costing anything to send (email, not SMS). category_id alone
    scopes it, same as Vote/uq_vote_per_category — a category_id already
    uniquely implies its parent (election or event)."""
    __tablename__ = "voter_otps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.category_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    otp_code: Mapped[str] = mapped_column(String(6), nullable=False,
                                          default=lambda: f"{random.randint(0, 999999):06d}")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class EligibleVoter(Base):
    __tablename__ = "eligible_voters"
    __table_args__ = (
        UniqueConstraint("election_id", "user_id", name="uq_eligible_voter"),
    )

    eligible_voter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    election_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("elections.election_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    notified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    election: Mapped["Election"] = relationship(
        back_populates="eligible_voters", lazy="select"
    )
    user: Mapped["User"] = relationship(
        back_populates="eligible_voters", lazy="select"
    )
