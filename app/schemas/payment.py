from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator


class InitiateVotePaymentRequest(BaseModel):
    category_id: UUID
    candidate_ids: List[UUID]
    email: EmailStr
    # Custom amount in pesewas for multi-vote elections.
    # If omitted, defaults to VOTE_PRICE (one vote = ₵1).
    amount_pesewas: Optional[int] = None

    @field_validator("amount_pesewas")
    @classmethod
    def validate_amount(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 50:
            raise ValueError("Minimum payment is 50 pesewas (₵0.50)")
        return v


class VotePaymentInitResponse(BaseModel):
    reference: str
    access_code: str
    public_key: str
    amount: int       # in pesewas
    currency: str = "GHS"
    vote_count: int = 1  # number of vote records this payment buys


class VerifyAndCastRequest(BaseModel):
    reference: str


class TransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: UUID
    reference: str
    election_id: Optional[UUID] = None
    event_id: Optional[UUID] = None
    category_id: UUID
    email: Optional[str]
    amount: int
    currency: str
    status: str
    created_at: datetime
