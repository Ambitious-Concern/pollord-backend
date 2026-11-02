import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class AppLogAlert(Base):
    """Admin-defined alert rule: fires when AppLog volume or level crosses a threshold."""

    __tablename__ = "app_log_alerts"

    alert_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))

    # trigger_type: new_critical | level_threshold | new_issue | rate_spike
    trigger_type: Mapped[str] = mapped_column(String(30))
    # optional level filter (applies to level_threshold + new_issue)
    level: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # count needed before the rule fires (for level_threshold / rate_spike)
    threshold_count: Mapped[int] = mapped_column(Integer, default=1)
    # rolling window in minutes (for rate_spike)
    window_minutes: Mapped[int] = mapped_column(Integer, default=60)

    # delivery channel
    channel_type: Mapped[str] = mapped_column(String(20))        # email | webhook
    channel_destination: Mapped[str] = mapped_column(String(500))

    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_triggered_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())
