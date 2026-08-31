from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


class PayoutRequestCreate(BaseModel):
    """The organizer's payout destination — collected at request time."""

    payout_method: Literal["mobile_money"] = "mobile_money"
    recipient_name: str
    mobile_network: str  # Paystack bank_code, from GET /payouts/mobile-money-networks
    mobile_number: str

    @field_validator("recipient_name", "mobile_network", "mobile_number")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("This field is required")
        return v.strip()


class PayoutRequestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payout_request_id: UUID
    event_id: Optional[UUID] = None
    event_title: str = ""
    election_id: Optional[UUID] = None
    election_title: str = ""
    organizer_id: UUID
    organizer_name: str = ""
    organizer_email: str = ""
    amount: Decimal
    status: str
    admin_notes: Optional[str] = None
    payout_method: Optional[str] = None
    recipient_name: Optional[str] = None
    mobile_network: Optional[str] = None
    mobile_number: Optional[str] = None
    transfer_reference: Optional[str] = None
    transfer_status: Optional[str] = None
    requested_at: datetime
    reviewed_at: Optional[datetime] = None


class PayoutAvailableResponse(BaseModel):
    event_id: Optional[UUID] = None
    election_id: Optional[UUID] = None
    gross_revenue: Decimal
    already_requested: Decimal
    available: Decimal
    has_pending_request: bool


class PayoutReviewRequest(BaseModel):
    status: str  # "paid" or "rejected"
    admin_notes: Optional[str] = None


class MobileMoneyNetwork(BaseModel):
    name: str
    code: str
