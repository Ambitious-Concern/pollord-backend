from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

TICKET_CATEGORIES = (
    "Billing & Payouts",
    "Technical issue",
    "Voting & Elections",
    "Events & Ticketing",
    "Account & Verification",
    "Live Chat",
    "Other",
)


class SupportTicketCreate(BaseModel):
    category: str
    subject: str
    message: str

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        if v not in TICKET_CATEGORIES:
            raise ValueError(f"category must be one of: {TICKET_CATEGORIES}")
        return v


class SupportMessageCreate(BaseModel):
    text: str


class SupportMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    message_id: UUID
    sender_type: Literal["customer", "agent"]
    text: str
    created_at: datetime


class SupportTicketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticket_id: UUID
    subject: str
    category: str
    channel: Literal["email", "chat"] = "email"
    status: str
    priority: bool
    assigned_agent_id: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime
    messages: List[SupportMessageResponse] = []


class UpdateTicketStatus(BaseModel):
    status: Literal["Open", "Replied", "Resolved"]
