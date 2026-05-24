from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


# --- Candidate schemas ---


class CandidateCreate(BaseModel):
    name: str
    email: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    display_order: int = 0


class CandidateUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    display_order: Optional[int] = None


class CandidateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    candidate_id: UUID
    election_id: UUID
    name: str
    short_code: Optional[str] = None
    email: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    display_order: int


# --- Election schemas ---


class ElectionSettings(BaseModel):
    """Client-configurable election settings."""
    visibility: str = "public"  # public | private
    access_code: Optional[str] = None
    allow_result_viewing: str = "after_end"  # live | after_end | admin_only
    require_verification: bool = False
    anonymous_results: bool = True
    allow_abstain: bool = False
    show_candidate_count: bool = False
    randomize_candidate_order: bool = False
    enable_notifications: bool = True
    max_selections: Optional[int] = None
    allow_revoting: bool = False
    vote_price: Optional[int] = None  # pesewas; None = inherit global platform price

    @field_validator("vote_price")
    @classmethod
    def validate_vote_price(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return v
        if v < 100:
            raise ValueError("Vote price must be at least 100 pesewas (₵1)")
        if v % 100 != 0:
            raise ValueError("Vote price must be a multiple of 100 pesewas")
        return v

    @field_validator("visibility")
    @classmethod
    def validate_visibility(cls, v: str) -> str:
        if v not in ("public", "private"):
            raise ValueError("Visibility must be 'public' or 'private'")
        return v

    @field_validator("allow_result_viewing")
    @classmethod
    def validate_result_viewing(cls, v: str) -> str:
        if v not in ("live", "after_end", "admin_only"):
            raise ValueError("Must be 'live', 'after_end', or 'admin_only'")
        return v


class ElectionCreate(BaseModel):
    title: str
    description: Optional[str] = None
    election_type: str
    start_datetime: datetime
    end_datetime: datetime
    banner_image_url: Optional[str] = None
    settings: Optional[ElectionSettings] = None

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
    election_type: Optional[str] = None
    start_datetime: Optional[datetime] = None
    end_datetime: Optional[datetime] = None
    banner_image_url: Optional[str] = None
    settings: Optional[ElectionSettings] = None


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
    banner_image_url: Optional[str] = None
    visibility: str = "public"
    access_code: Optional[str] = None
    allow_result_viewing: str = "after_end"
    require_verification: bool = False
    anonymous_results: bool = True
    allow_abstain: bool = False
    show_candidate_count: bool = False
    randomize_candidate_order: bool = False
    enable_notifications: bool = True
    max_selections: Optional[int] = None
    allow_revoting: bool = False
    vote_price: Optional[int] = None  # None means election uses the global platform price


class ElectionPublicResponse(BaseModel):
    """Slimmed-down response for public (unauthenticated) listing."""
    model_config = ConfigDict(from_attributes=True)

    election_id: UUID
    title: str
    description: Optional[str] = None
    election_type: str
    start_datetime: datetime
    end_datetime: datetime
    status: str
    created_by: UUID
    banner_image_url: Optional[str] = None
    visibility: str = "public"
    created_at: datetime


class ElectionWithCandidates(ElectionResponse):
    candidates: List[CandidateResponse] = []
    effective_vote_price: int = 100  # resolved price (global or per-election), always set


# --- Eligible Voter schemas ---


class EligibleVoterAdd(BaseModel):
    user_ids: List[UUID]


class EligibleVoterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    eligible_voter_id: UUID
    election_id: UUID
    user_id: UUID
    notified_at: Optional[datetime] = None
    created_at: datetime
