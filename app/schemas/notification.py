from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict

NotificationType = Literal["vote", "ticket", "payout", "nomination", "verification", "alert"]


class NotificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    notification_id: UUID
    type: NotificationType
    title: str
    description: str
    link: Optional[str] = None
    is_read: bool
    created_at: datetime


class NotificationListResponse(BaseModel):
    items: List[NotificationResponse]
    unread_count: int


class MarkAllReadResponse(BaseModel):
    marked: int
