from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


class PayoutRequestCreate(BaseModel):
    """The organizer's payout destination — collected at request time.
    destination_code is a Paystack bank_code either way: a mobile network
    code (from GET /payouts/mobile-money-networks) when payout_method is
    "mobile_money", or a real bank code (from GET /payouts/banks) when it's
    "bank_account". amount defaults to the full available balance when
    omitted, letting an organizer request only part of it instead."""

    payout_method: Literal["mobile_money", "bank_account"] = "mobile_money"
    recipient_name: str
    destination_code: str
    destination_number: str
    amount: Optional[Decimal] = None

    @field_validator("recipient_name", "destination_code", "destination_number")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("This field is required")
        return v.strip()

    @field_validator("amount")
    @classmethod
    def positive_amount(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is not None and v <= 0:
            raise ValueError("amount must be greater than zero")
        return v


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
    express: bool = False
    admin_notes: Optional[str] = None
    payout_method: Optional[str] = None
    recipient_name: Optional[str] = None
    destination_code: Optional[str] = None
    destination_number: Optional[str] = None
    transfer_reference: Optional[str] = None
    transfer_status: Optional[str] = None
    requested_at: datetime
    reviewed_at: Optional[datetime] = None


class PayoutAvailableResponse(BaseModel):
    event_id: Optional[UUID] = None
    election_id: Optional[UUID] = None
    gross_revenue: Decimal
    # Pollord's commission on gross_revenue — vote commission for elections,
    # ticket commission for events (see PayoutService._vote_platform_fee /
    # _ticket_platform_fee).
    platform_fee: Decimal = Decimal("0")
    already_requested: Decimal
    available: Decimal
    has_pending_request: bool


class PayoutSourceAvailability(BaseModel):
    """One row per election/event the organizer has ever earned money from —
    the bulk equivalent of PayoutAvailableResponse, for the sources table
    that lists every source at once instead of one at a time."""

    source_id: UUID
    source_type: Literal["election", "event"]
    source_name: str
    gross_revenue: Decimal
    # Same as PayoutAvailableResponse.platform_fee.
    platform_fee: Decimal = Decimal("0")
    already_requested: Decimal
    available: Decimal
    has_pending_request: bool


class PayoutReviewRequest(BaseModel):
    status: str  # "paid" or "rejected"
    admin_notes: Optional[str] = None


class PayoutDestinationOption(BaseModel):
    """One selectable destination for a payout — a mobile money network or a
    bank, depending on which list endpoint returned it."""

    name: str
    code: str
