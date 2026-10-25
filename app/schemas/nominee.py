from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel


class NomineeResponse(BaseModel):
    """One candidate, wherever it lives (election or event), with the same
    real vote-tally/rank fields as elections.py's list_candidates — just
    computed once across every one of the caller's elections and events
    instead of one at a time."""

    candidate_id: UUID
    name: str
    short_code: Optional[str] = None
    image_url: Optional[str] = None
    category_id: UUID
    category_name: str
    parent_id: UUID
    parent_type: Literal["election", "event"]
    parent_title: str
    vote_count: int
    category_rank: int
    created_at: datetime


class NomineeListResponse(BaseModel):
    items: List[NomineeResponse]
