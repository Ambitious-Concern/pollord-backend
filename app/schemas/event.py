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
    # "organizer" (default): commission comes out of the payout. "buyer":
    # commission is added on top as a surcharge, organizer nets sticker price.
    fee_payer: str = "organizer"
    # Independent voting window — None means "fall back to the event's own
    # start time through end of that calendar day" (see EventResponse).
    voting_starts_at: Optional[datetime] = None
    voting_ends_at: Optional[datetime] = None
    # Whether ticket check-in is open.
    scan_enabled: bool = True

    @field_validator("fee_payer")
    @classmethod
    def validate_fee_payer(cls, v: str) -> str:
        if v not in ("organizer", "buyer"):
            raise ValueError("fee_payer must be 'organizer' or 'buyer'")
        return v


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
    fee_payer: Optional[str] = None
    voting_starts_at: Optional[datetime] = None
    voting_ends_at: Optional[datetime] = None
    scan_enabled: Optional[bool] = None

    @field_validator("fee_payer")
    @classmethod
    def validate_fee_payer(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("organizer", "buyer"):
            raise ValueError("fee_payer must be 'organizer' or 'buyer'")
        return v


class EventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: UUID
    title: str
    slug: Optional[str] = None
    ussd_code: Optional[str] = None
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
    fee_payer: str = "organizer"
    voting_starts_at: Optional[datetime] = None
    voting_ends_at: Optional[datetime] = None
    scan_enabled: bool = True
    # Chosen at launch, locked in until the event closes (see core/plans.py)
    # — determines the commission rate, not an org-wide setting.
    plan: str = "standard"
    created_by: UUID
    created_at: datetime
    updated_at: datetime


class EventSummaryResponse(EventResponse):
    """The organizer dashboard's list view — adds bulk-computed aggregate
    stats on top of the plain event row, mirroring ElectionSummaryResponse."""

    tickets_sold: int = 0
    # Summed real ticket-type capacity (quantity_available + quantity_sold) —
    # kept distinct from the inherited `capacity` field (Event.capacity),
    # which organizers often leave unset (None), unlike this, always an int.
    ticket_capacity: int = 0
    lowest_ticket_price: Optional[Decimal] = None
    has_voting: bool = False
    revenue_ghs: str = "0.00"
    revenue_growth_pct: float = 0.0


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


class TicketTypeWithEventResponse(TicketTypeResponse):
    """One ticket type plus its parent event's title — the cross-event
    Ticket Types dashboard page's row, vs. the per-event list which doesn't
    need to repeat the event it's already scoped to."""

    event_title: str = ""


class EventWithTicketTypes(EventResponse):
    ticket_types: List[TicketTypeResponse] = []
    effective_vote_price: int = 100  # resolved price (global or per-event), always set


class EventWithCategories(EventResponse):
    categories: List[CategoryWithCandidates] = []
    effective_vote_price: int = 100
