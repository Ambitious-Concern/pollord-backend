from datetime import datetime
from decimal import Decimal
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict

TemplateKind = Literal["election", "event"]
TemplateVisibility = Literal["private", "public"]
VotingType = Literal["free", "paid"]


class TemplateTicketTier(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    price: Decimal
    description: str = ""
    available: int


class TemplateCreate(BaseModel):
    kind: TemplateKind
    # The real election/event this template is snapshotted from — must be
    # owned by the caller. Its categories/ticket types are captured as data
    # here, not referenced live.
    source_id: UUID
    title: str
    description: Optional[str] = None
    visibility: TemplateVisibility = "private"


class TemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    template_id: UUID
    kind: TemplateKind
    title: str
    description: Optional[str] = None
    visibility: TemplateVisibility
    categories: List[str] = []
    ticket_tiers: List[TemplateTicketTier] = []
    voting_type: Optional[VotingType] = None
    vote_price_pesewas: Optional[int] = None
    # Whether the caller owns/can-manage this template (vs. seeing it only
    # because it's someone else's public one) — the frontend's delete
    # action is only ever shown for a template you can actually delete.
    is_mine: bool = True
    created_at: datetime
