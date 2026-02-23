import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.vote import Vote, VoteReceipt


class Election(TimestampMixin, Base):
    __tablename__ = "elections"

    election_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    election_type: Mapped[str] = mapped_column(String(50), nullable=False)
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

    # Relationships
    creator: Mapped["User"] = relationship(
        back_populates="created_elections", lazy="select"
    )
    candidates: Mapped[List["Candidate"]] = relationship(
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


class Candidate(TimestampMixin, Base):
    __tablename__ = "candidates"

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    election_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("elections.election_id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    # Relationships
    election: Mapped["Election"] = relationship(
        back_populates="candidates", lazy="select"
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
