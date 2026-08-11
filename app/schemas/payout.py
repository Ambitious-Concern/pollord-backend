from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class PayoutRequestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payout_request_id: UUID
    event_id: UUID
    event_title: str = ""
    organizer_id: UUID
    organizer_name: str = ""
    organizer_email: str = ""
    amount: Decimal
    status: str
    admin_notes: Optional[str] = None
    requested_at: datetime
    reviewed_at: Optional[datetime] = None


class PayoutAvailableResponse(BaseModel):
    event_id: UUID
    gross_revenue: Decimal
    already_requested: Decimal
    available: Decimal
    has_pending_request: bool


class PayoutReviewRequest(BaseModel):
    status: str  # "paid" or "rejected"
    admin_notes: Optional[str] = None
