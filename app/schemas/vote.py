from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CastVote(BaseModel):
    election_id: UUID
    candidate_ids: Optional[List[UUID]] = None
    candidate_short_codes: Optional[List[str]] = None


class VoteReceiptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    receipt_code: str
    election_id: UUID
    issued_at: datetime


class CandidateResult(BaseModel):
    candidate_id: UUID
    name: str
    vote_count: int
    percentage: float


class ElectionResults(BaseModel):
    election_id: UUID
    title: str
    election_type: str
    total_votes: int
    total_eligible_voters: int
    turnout_percentage: float
    results: List[CandidateResult]
    status: str
