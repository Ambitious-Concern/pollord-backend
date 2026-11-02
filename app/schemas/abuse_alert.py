from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel

from app.models.abuse_alert import AlertSeverity, AlertStatus, AlertType


class AbuseAlertResponse(BaseModel):
    id: UUID
    alert_type: AlertType
    severity: AlertSeverity
    source_ip: Optional[str] = None
    source_user_id: Optional[UUID] = None
    source_user_name: Optional[str] = None
    source_user_email: Optional[str] = None
    target_path: Optional[str] = None
    detail: Optional[dict] = None
    status: AlertStatus
    created_at: datetime
    resolved_at: Optional[datetime] = None
    resolved_by_name: Optional[str] = None

    model_config = {"from_attributes": True}


class AbuseAlertUpdate(BaseModel):
    status: AlertStatus


class AbuseAlertStats(BaseModel):
    total: int
    critical: int
    unresolved: int
    resolved_today: int
