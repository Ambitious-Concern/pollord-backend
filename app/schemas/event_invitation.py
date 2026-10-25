from datetime import date, datetime, time
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class EventInvitationCreate(BaseModel):
    email: EmailStr
    name: Optional[str] = Field(default=None, max_length=255)


class EventInvitationResponse(BaseModel):
    """Organizer-facing view of an invitation they sent."""

    model_config = ConfigDict(from_attributes=True)

    invitation_id: UUID
    event_id: UUID
    email: str
    name: Optional[str]
    status: str
    expires_at: datetime
    created_at: datetime


class EventInvitationPublicResponse(BaseModel):
    """Public token-lookup view — enough event context for the invitee to
    decide before RSVPing, without exposing organizer-only data."""

    invitation_id: UUID
    email: str
    name: Optional[str]
    status: str
    expires_at: datetime
    event_id: UUID
    event_title: str
    event_date: date
    event_time: time
    location: str
    banner_image_url: Optional[str] = None


class EventRSVPRequest(BaseModel):
    response: Literal["accepted", "declined"]
