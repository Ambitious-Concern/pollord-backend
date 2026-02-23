from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


class CandidateCreate(BaseModel):
    name: str
    description: Optional[str] = None
    image_url: Optional[str] = None
    display_order: int = 0


class CandidateUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    display_order: Optional[int] = None


class CandidateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    candidate_id: UUID
    election_id: UUID
    name: str
    description: Optional[str] = None
    image_url: Optional[str] = None
    display_order: int


class ElectionCreate(BaseModel):
    title: str
    description: Optional[str] = None
    election_type: str
    start_datetime: datetime
    end_datetime: datetime

    @field_validator("election_type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        allowed = {"single_choice", "multiple_choice", "ranked"}
        if v not in allowed:
            raise ValueError(f"Election type must be one of: {allowed}")
        return v

    @field_validator("end_datetime")
    @classmethod
    def validate_dates(cls, v: datetime, info) -> datetime:
        start = info.data.get("start_datetime")
        if start and v <= start:
            raise ValueError("End datetime must be after start datetime")
        return v


class ElectionUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    start_datetime: Optional[datetime] = None
    end_datetime: Optional[datetime] = None


class ElectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    election_id: UUID
    title: str
    description: Optional[str] = None
    election_type: str
    start_datetime: datetime
    end_datetime: datetime
    status: str
    created_by: UUID
    created_at: datetime
    updated_at: datetime


class ElectionWithCandidates(ElectionResponse):
    candidates: List[CandidateResponse] = []


class EligibleVoterAdd(BaseModel):
    user_ids: List[UUID]


class EligibleVoterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    eligible_voter_id: UUID
    election_id: UUID
    user_id: UUID
    notified_at: Optional[datetime] = None
    created_at: datetime
