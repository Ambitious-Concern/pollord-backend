from datetime import datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator


class TicketPurchaseItem(BaseModel):
    ticket_type_id: UUID
    quantity: int

    @field_validator("quantity")
    @classmethod
    def validate_quantity(cls, v: int) -> int:
        if v < 1:
            raise ValueError("Quantity must be at least 1")
        return v


class TicketPurchaseRequest(BaseModel):
    event_id: UUID
    items: List[TicketPurchaseItem]

    @field_validator("items")
    @classmethod
    def validate_items(cls, v: list) -> list:
        if not v:
            raise ValueError("At least one ticket item is required")
        return v


class TicketPaymentInitResponse(BaseModel):
    reference: str
    access_code: str
    public_key: str
    amount: Decimal
    currency: str

    @field_serializer("amount")
    def _serialize_amount(self, v: Decimal, _info) -> float:
        # Pydantic v2 serializes Decimal as a string in JSON mode by default;
        # the frontend/tests expect a plain JSON number here.
        return float(v)


class VerifyAndPurchaseRequest(BaseModel):
    reference: str


class TicketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticket_id: UUID
    ticket_code: str
    event_id: UUID
    ticket_type_id: UUID
    ticket_status: str
    purchase_date: datetime
    used_at: Optional[datetime] = None


class TicketDetailResponse(TicketResponse):
    event_title: str = ""
    ticket_type_name: str = ""
    attendee_name: str = ""


class TicketPurchaseResponse(BaseModel):
    purchase_id: UUID
    event_id: UUID
    total_amount: Decimal
    payment_status: str
    tickets: List[TicketResponse]
    purchased_at: datetime


class TicketValidation(BaseModel):
    ticket_code: str


class TicketValidationResponse(BaseModel):
    valid: bool
    message: str
    ticket: Optional[TicketResponse] = None
    attendee_name: Optional[str] = None
    event_title: Optional[str] = None
    ticket_type: Optional[str] = None
