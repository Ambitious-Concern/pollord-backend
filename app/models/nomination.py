import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, Text, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.election import Category, Election
    from app.models.user import User

FIELD_TYPES = ("short-text", "paragraph", "multiple-choice", "photo")
SUBMISSION_SOURCES = ("public", "private", "manual")
SUBMISSION_STATUSES = ("pending", "approved", "rejected")


class NominationForm(TimestampMixin, Base):
    """A shareable form nominators fill in to suggest someone for an
    election's categories. Always scoped to exactly one election — the
    category question on the live form is generated from that election's
    real categories, never hand-maintained (see NominationFormField, which
    only stores the *custom* questions an organizer adds)."""

    __tablename__ = "nomination_forms"

    form_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    election_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("elections.election_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # "public" = anyone with the link; "private" = same link, but a
    # submission through it is tagged accordingly for the review queue's
    # public/private/manual split — enforcement of "private" access is a
    # frontend/future concern, not modeled here yet.
    visibility: Mapped[str] = mapped_column(String(10), default="public", nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )

    election: Mapped["Election"] = relationship(lazy="select")
    fields: Mapped[List["NominationFormField"]] = relationship(
        back_populates="form", order_by="NominationFormField.display_order",
        cascade="all, delete-orphan",
    )


class NominationFormField(Base):
    """One custom question on a nomination form. display_order is rewritten
    wholesale on every save — the builder always submits its full field list
    in the order it wants, not incremental moves."""

    __tablename__ = "nomination_form_fields"

    field_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    form_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nomination_forms.form_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    field_type: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    placeholder: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Multiple-choice options, in display order. Null for every other type.
    options: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    form: Mapped["NominationForm"] = relationship(back_populates="fields")


class NominationSubmission(Base):
    """One nominator's suggestion of a person for a category — reviewed as
    part of its (category_id, nominee_name) group (see
    NominationService.list_groups) and, on approval, turned into a real
    Candidate. form_id is null for a "manual" submission an organizer typed
    in directly, with no form involved at all."""

    __tablename__ = "nomination_submissions"

    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    form_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nomination_forms.form_id", ondelete="SET NULL"),
        nullable=True,
    )
    # Denormalized (also reachable via form_id -> election_id) so manual
    # submissions — which have no form — still carry it directly, and so
    # RLS can scope this table the same simple way as any other
    # election-child table.
    election_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("elections.election_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.category_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    nominee_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    # {field_id: answer} for the form's custom questions at submission time —
    # a snapshot, since fields can change or be deleted after the fact.
    answers: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    submitter_ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    submitter_user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    election: Mapped["Election"] = relationship(lazy="select")
    category: Mapped["Category"] = relationship(lazy="select")
