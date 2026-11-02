import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class AlertType(str, enum.Enum):
    RATE_LIMIT = "RATE_LIMIT"
    BRUTE_FORCE = "BRUTE_FORCE"
    DDOS = "DDOS"
    SUSPICIOUS_IP = "SUSPICIOUS_IP"
    VOTING_ANOMALY = "VOTING_ANOMALY"
    PAYMENT_FRAUD = "PAYMENT_FRAUD"
    OTP_ABUSE = "OTP_ABUSE"


class AlertSeverity(str, enum.Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AlertStatus(str, enum.Enum):
    NEW = "NEW"
    INVESTIGATING = "INVESTIGATING"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class AbuseAlert(Base):
    """One row per detected abuse event. Written by middleware/services;
    status is updated by admins via the Security > Abuse Alerts console."""

    __tablename__ = "abuse_alerts"
    __table_args__ = (
        Index("idx_abuse_alert_severity", "severity"),
        Index("idx_abuse_alert_status", "status"),
        Index("idx_abuse_alert_created", "created_at"),
        Index("idx_abuse_alert_source_ip", "source_ip"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    alert_type: Mapped[AlertType] = mapped_column(String(30), nullable=False)
    severity: Mapped[AlertSeverity] = mapped_column(String(10), nullable=False)
    source_ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    source_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )
    target_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status: Mapped[AlertStatus] = mapped_column(
        String(15), nullable=False, default=AlertStatus.NEW, server_default="NEW"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )

    source_user: Mapped[Optional["User"]] = relationship(foreign_keys=[source_user_id])
    resolver: Mapped[Optional["User"]] = relationship(foreign_keys=[resolved_by])
