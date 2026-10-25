from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EventBroadcastCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1)


class EventBroadcastResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    broadcast_id: UUID
    event_id: UUID
    subject: str
    body: str
    recipient_count: int
    sent_count: int
    failed_count: int
    status: str
    created_at: datetime
