from datetime import date, datetime, time
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.schemas.election import CategoryWithCandidates


class EventCreate(BaseModel):
    title: str
    slug: Optional[str] = None  # auto-generated from title if omitted
    description: Optional[str] = None
    event_date: date
    event_time: time
    location: str
    category: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    capacity: Optional[int] = None
    banner_image_url: Optional[str] = None
    # Whether buyers see remaining-ticket counts. Sold-out is always shown.
    show_ticket_counts: bool = True
    # Category-voting settings — independent of ticket sales. vote_price is
    # pesewas, None = inherit the global platform price (mirrors Election).
    vote_price: Optional[int] = None
    allow_revoting: bool = False


class EventUpdate(BaseModel):
    title: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    event_date: Optional[date] = None
    event_time: Optional[time] = None
    location: Optional[str] = None
    category: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    capacity: Optional[int] = None
    banner_image_url: Optional[str] = None
    show_ticket_counts: Optional[bool] = None
    vote_price: Optional[int] = None
    allow_revoting: Optional[bool] = None


class EventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: UUID
    title: str
    slug: Optional[str] = None
    description: Optional[str] = None
    event_date: date
    event_time: time
    location: str
    category: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    capacity: Optional[int] = None
    banner_image_url: Optional[str] = None
    status: str
    show_ticket_counts: bool = True
    vote_price: Optional[int] = None
    allow_revoting: bool = False
    created_by: UUID
    created_at: datetime
    updated_at: datetime


class TicketTypeCreate(BaseModel):
    type_name: str
    description: Optional[str] = None
    price: Decimal
    quantity_available: int
    sales_start_datetime: Optional[datetime] = None
    sales_end_datetime: Optional[datetime] = None
    max_per_user: int = 5

    @field_validator("price")
    @classmethod
    def validate_price(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("Price cannot be negative")
        return v

    @field_validator("quantity_available")
    @classmethod
    def validate_quantity(cls, v: int) -> int:
        if v < 1:
            raise ValueError("Quantity must be at least 1")
        return v


class TicketTypeUpdate(BaseModel):
    type_name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[Decimal] = None
    quantity_available: Optional[int] = None
    sales_start_datetime: Optional[datetime] = None
    sales_end_datetime: Optional[datetime] = None
    max_per_user: Optional[int] = None
    status: Optional[str] = None


class TicketTypeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticket_type_id: UUID
    event_id: UUID
    type_name: str
    description: Optional[str] = None
    price: Decimal
    quantity_available: int
    quantity_sold: int
    sales_start_datetime: Optional[datetime] = None
    sales_end_datetime: Optional[datetime] = None
    max_per_user: int
    status: str


class EventWithTicketTypes(EventResponse):
    ticket_types: List[TicketTypeResponse] = []


class EventWithCategories(EventResponse):
    categories: List[CategoryWithCandidates] = []
    effective_vote_price: int = 100
