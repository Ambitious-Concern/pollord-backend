import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class SuggestedFix(BaseModel):
    title: str
    detail: str
    confidence: float  # 0.0–1.0


class AppLogResponse(BaseModel):
    log_id: uuid.UUID
    level: str
    source: str
    logger_name: str
    message: str
    traceback: Optional[str]
    fingerprint: str
    release: Optional[str] = None

    request_id: Optional[str]
    request_method: Optional[str]
    request_path: Optional[str]
    request_user_email: Optional[str]
    request_ip: Optional[str]
    http_status: Optional[int]

    extra: Optional[dict[str, Any]]

    resolved: bool
    resolved_at: Optional[datetime]
    resolved_by_email: Optional[str]

    occurrence_count: int
    affected_users_count: int = 0
    first_seen_at: datetime
    last_seen_at: datetime
    created_at: datetime

    suggested_fixes: list[SuggestedFix] = []

    model_config = {"from_attributes": True}


class AppLogListResponse(BaseModel):
    items: list[AppLogResponse]
    total: int


class AppLogStats(BaseModel):
    total: int
    critical: int
    errors: int
    warnings: int
    unresolved: int
    resolved_today: int


class AppLogTrendBucket(BaseModel):
    hour: datetime
    level: str
    count: int


class AppLogTrendResponse(BaseModel):
    buckets: list[AppLogTrendBucket]


class ResolveRequest(BaseModel):
    resolved_by_email: str
