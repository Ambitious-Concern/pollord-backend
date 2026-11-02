import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class AppLogAlertResponse(BaseModel):
    alert_id: uuid.UUID
    name: str
    trigger_type: str
    level: Optional[str]
    threshold_count: int
    window_minutes: int
    channel_type: str
    channel_destination: str
    cooldown_minutes: int
    enabled: bool
    last_triggered_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class AppLogAlertCreate(BaseModel):
    name: str
    trigger_type: str       # new_critical | level_threshold | new_issue | rate_spike
    level: Optional[str] = None
    threshold_count: int = 1
    window_minutes: int = 60
    channel_type: str       # email | webhook
    channel_destination: str
    cooldown_minutes: int = 60


class AppLogAlertUpdate(BaseModel):
    name: Optional[str] = None
    trigger_type: Optional[str] = None
    level: Optional[str] = None
    threshold_count: Optional[int] = None
    window_minutes: Optional[int] = None
    channel_type: Optional[str] = None
    channel_destination: Optional[str] = None
    cooldown_minutes: Optional[int] = None
    enabled: Optional[bool] = None
