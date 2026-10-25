from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class ActivityActor(BaseModel):
    user_id: Optional[UUID] = None
    name: str
    role: Optional[str] = None
    is_system: bool = False


class OrgActivityLogResponse(BaseModel):
    log_id: UUID
    actor: ActivityActor
    category: str
    verb: str
    target_name: str
    detail: Optional[str] = None
    created_at: datetime
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
