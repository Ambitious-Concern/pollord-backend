import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.election import Election
    from app.models.user import User


class Vote(Base):
    __tablename__ = "votes"
    __table_args__ = (
        UniqueConstraint("election_id", "voter_hash", name="uq_vote_per_election"),
    )

    vote_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    election_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("elections.election_id"), nullable=False,
        index=True,
    )
    voter_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    vote_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    vote_signature: Mapped[str] = mapped_column(String(128), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cast_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    election: Mapped["Election"] = relationship(
        back_populates="votes", lazy="select"
    )


class VoteReceipt(Base):
    __tablename__ = "vote_receipts"

    receipt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    election_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("elections.election_id"), nullable=False
    )
    receipt_code: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    user: Mapped["User"] = relationship(
        back_populates="vote_receipts", lazy="select"
    )
    election: Mapped["Election"] = relationship(
        back_populates="vote_receipts", lazy="select"
    )
