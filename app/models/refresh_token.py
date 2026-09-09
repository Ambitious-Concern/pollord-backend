import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class RefreshToken(Base):
    """One row per issued refresh token, keyed by its JWT `jti` claim.

    Enables single-use rotation with reuse detection: refresh_token() marks
    the presented token's row revoked and issues a fresh one. If a token
    whose row is already revoked (or missing) is ever presented again, it's
    a strong signal of theft/replay, not just an expired session — the
    caller should treat that as a reason to revoke every token for the user
    (via User.token_version), not just deny the one request.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("idx_refresh_token_user", "user_id"),
    )

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship()
