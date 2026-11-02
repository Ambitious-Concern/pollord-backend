import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

BACKUP_STATUSES = ("pending", "running", "completed", "failed")
BACKUP_TYPES = ("scheduled", "manual")


class BackupRecord(TimestampMixin, Base):
    """Tracks every pg_dump run — both scheduled (Celery beat) and manual
    (admin-triggered). The Celery task writes status transitions so the
    UI can poll a single record from pending → running → completed/failed
    without scraping logs."""

    __tablename__ = "backup_records"
    __table_args__ = (
        Index("ix_backup_records_created_at", "created_at"),
        Index("ix_backup_records_status", "status"),
    )

    backup_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    label: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    type: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    size_bytes: Mapped[Optional[int]] = mapped_column(nullable=True)
    filename: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    storage_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    checksum_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # null = scheduled beat; set = admin who clicked "Create Backup"
    triggered_by_email: Mapped[Optional[str]] = mapped_column(String(254), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
