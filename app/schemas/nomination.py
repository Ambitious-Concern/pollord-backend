from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

FieldType = Literal["short-text", "paragraph", "multiple-choice", "photo"]
Visibility = Literal["public", "private"]
SubmissionSource = Literal["public", "private", "manual"]
SubmissionStatus = Literal["pending", "approved", "rejected"]


class NominationFormFieldInput(BaseModel):
    """One custom question, as sent by the builder on every save — the
    whole field list is replaced wholesale, so there's no separate
    create/update/reorder shape."""

    field_type: FieldType
    label: str
    placeholder: Optional[str] = None
    required: bool = False
    options: Optional[List[str]] = None

    @field_validator("options")
    @classmethod
    def options_required_for_multiple_choice(cls, v, info):
        if info.data.get("field_type") == "multiple-choice" and not v:
            raise ValueError("multiple-choice fields need at least one option")
        return v


class NominationFormFieldResponse(NominationFormFieldInput):
    model_config = ConfigDict(from_attributes=True)

    field_id: UUID
    display_order: int


class NominationFormCreate(BaseModel):
    title: str
    description: Optional[str] = None
    visibility: Visibility = "public"
    fields: List[NominationFormFieldInput] = []


class NominationFormUpdate(BaseModel):
    title: str
    description: Optional[str] = None
    visibility: Visibility = "public"
    fields: List[NominationFormFieldInput] = []


class NominationFormResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    form_id: UUID
    election_id: UUID
    title: str
    description: Optional[str] = None
    visibility: Visibility
    slug: str
    url: str = ""
    # Populated only by the cross-election aggregate list — a single-election
    # list already has this from context, so it's left blank there.
    election_title: str = ""
    fields: List[NominationFormFieldResponse] = []
    created_at: datetime
    updated_at: datetime


class PublicNominationFormResponse(BaseModel):
    """What a nominator sees — the two locked fields (category, nominee
    name) are synthesized here from the election's real categories, never
    stored (see NominationForm's docstring)."""

    form_id: UUID
    title: str
    description: Optional[str] = None
    category_options: List[Dict[str, str]] = []  # [{category_id, name}]
    fields: List[NominationFormFieldResponse] = []


class NominationSubmissionCreate(BaseModel):
    category_id: UUID
    nominee_name: str
    answers: Dict[str, Any] = {}


class ManualNominationCreate(BaseModel):
    category_id: UUID
    nominee_name: str


class NominationGroupResponse(BaseModel):
    """A GROUP BY (election_id, category_id, nominee_name) view over
    submissions — the unit the review queue actually acts on, not a raw
    submission list."""

    election_id: UUID
    election_title: str
    category_id: UUID
    category_name: str
    nominee_name: str
    public_count: int
    private_count: int
    manual_count: int
    first_submitted_at: datetime
    status: SubmissionStatus


class NominationGroupReview(BaseModel):
    category_id: UUID
    nominee_name: str
    status: Literal["approved", "rejected"]


class NominationAnswerResponse(BaseModel):
    """One question+answer pair, resolved against the form field's label at
    submission time — shown even if left blank, so the reviewer sees the
    exact form the nominator was given, not just what they filled in."""

    field_id: UUID
    label: str
    field_type: FieldType
    value: Any = None


class NominationSubmissionResponse(BaseModel):
    """One nominator's full submission — the detail view behind a
    NominationGroupResponse row. answers is [] for a manual submission
    (no form was involved at all)."""

    submission_id: UUID
    category_id: UUID
    nominee_name: str
    source: SubmissionSource
    status: SubmissionStatus
    form_title: Optional[str] = None
    answers: List[NominationAnswerResponse] = []
    submitted_at: datetime
