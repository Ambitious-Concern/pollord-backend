import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class BackupRecordResponse(BaseModel):
    backup_id: uuid.UUID
    label: str
    type: str
    status: str
    size_bytes: Optional[int]
    filename: Optional[str]
    checksum_sha256: Optional[str]
    error: Optional[str]
    created_at: datetime
    completed_at: Optional[datetime]
    triggered_by_email: Optional[str]
    notes: Optional[str]

    model_config = {"from_attributes": True}


class BackupStatsResponse(BaseModel):
    total: int
    completed: int
    failed: int
    storage_bytes: int
    last_completed_at: Optional[datetime]
    next_scheduled: Optional[str]


class TriggerBackupRequest(BaseModel):
    notes: Optional[str] = None


class RestoreRequest(BaseModel):
    confirm_phrase: str  # must equal "RESTORE"
