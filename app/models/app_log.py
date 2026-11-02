import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class AppLog(Base):
    """Captured application-level log events (errors, warnings, info)."""

    __tablename__ = "app_logs"

    log_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    level: Mapped[str] = mapped_column(String(20))          # critical/error/warning/info/debug
    source: Mapped[str] = mapped_column(String(300))         # file:lineno
    logger_name: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    traceback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Short hash grouping identical errors together (Sentry fingerprint)
    fingerprint: Mapped[str] = mapped_column(String(16), index=True)

    # Request context (populated when captured from a live request)
    request_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    request_method: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    request_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    request_user_email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    request_ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Arbitrary JSON context (extra fields, env vars, etc.)
    extra: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Resolution lifecycle
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    resolved_by_email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Occurrence tracking — bumped each time the same fingerprint is seen
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(default=func.now())

    release: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=func.now())

    __table_args__ = (
        Index("ix_app_logs_level_created", "level", "created_at"),
        Index("ix_app_logs_resolved_created", "resolved", "created_at"),
    )
