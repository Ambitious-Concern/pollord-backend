from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr


class CastVote(BaseModel):
    category_id: UUID
    candidate_ids: Optional[List[UUID]] = None
    candidate_short_codes: Optional[List[str]] = None


class RequestVoteOTP(BaseModel):
    category_id: UUID
    email: EmailStr


class VerifyVoteOTPAndCast(BaseModel):
    category_id: UUID
    email: EmailStr
    otp: str
    candidate_ids: Optional[List[UUID]] = None
    candidate_short_codes: Optional[List[str]] = None


class VoteReceiptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    receipt_code: str
    # Exactly one is set — event-owned category votes have no election_id.
    election_id: Optional[UUID] = None
    event_id: Optional[UUID] = None
    issued_at: datetime


class CandidateResult(BaseModel):
    candidate_id: UUID
    name: str
    vote_count: int
    percentage: float


class CategoryResults(BaseModel):
    category_id: UUID
    name: str
    election_type: str
    total_votes: int
    results: List[CandidateResult]


class ElectionResults(BaseModel):
    election_id: UUID
    title: str
    total_votes: int
    total_eligible_voters: int
    turnout_percentage: float
    status: str
    categories: List[CategoryResults]


class EventResults(BaseModel):
    event_id: UUID
    title: str
    total_votes: int
    status: str
    categories: List[CategoryResults]
