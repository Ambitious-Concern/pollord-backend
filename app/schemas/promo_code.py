from datetime import date, datetime
from decimal import Decimal
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

DiscountType = Literal["percentage", "fixed_amount"]
PromoCodeStatus = Literal["active", "inactive", "expired"]


class PromoScopeInput(BaseModel):
    """Exactly one of election_id/event_id, mirroring PromoCodeScope's own
    CHECK constraint — enforced again here so a bad request 422s instead of
    reaching the database."""

    election_id: Optional[UUID] = None
    event_id: Optional[UUID] = None

    @field_validator("event_id")
    @classmethod
    def exactly_one_parent(cls, v, info):
        if bool(v) == bool(info.data.get("election_id")):
            raise ValueError("Provide exactly one of election_id or event_id")
        return v


class PromoScopeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    election_id: Optional[UUID] = None
    event_id: Optional[UUID] = None
    name: str = ""


class PromoCodeCreate(BaseModel):
    code: str
    discount_type: DiscountType
    discount_value: Decimal
    scope: List[PromoScopeInput] = []
    usage_limit: Optional[int] = None
    per_person_limit: int = 1
    valid_from: date
    valid_until: Optional[date] = None
    is_active: bool = True

    @field_validator("code")
    @classmethod
    def normalize_code(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("Code is required")
        return v

    @field_validator("discount_value")
    @classmethod
    def validate_discount_value(cls, v: Decimal, info) -> Decimal:
        if v <= 0:
            raise ValueError("Discount value must be positive")
        if info.data.get("discount_type") == "percentage" and v > 100:
            raise ValueError("Percentage discount cannot exceed 100")
        return v

    @field_validator("usage_limit")
    @classmethod
    def validate_usage_limit(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v < 1:
            raise ValueError("Usage limit must be at least 1")
        return v

    @field_validator("per_person_limit")
    @classmethod
    def validate_per_person_limit(cls, v: int) -> int:
        if v < 1:
            raise ValueError("Per-person limit must be at least 1")
        return v


class PromoCodeUpdate(PromoCodeCreate):
    pass


class PromoCodeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    promo_code_id: UUID
    code: str
    discount_type: DiscountType
    discount_value: Decimal
    scope: List[PromoScopeResponse] = []
    uses_count: int = 0
    usage_limit: Optional[int] = None
    per_person_limit: int
    valid_from: date
    valid_until: Optional[date] = None
    status: PromoCodeStatus
    created_at: datetime
