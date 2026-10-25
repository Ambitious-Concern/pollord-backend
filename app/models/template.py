import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.user import User

TEMPLATE_KINDS = ("election", "event")
TEMPLATE_VISIBILITIES = ("private", "public")


class Template(TimestampMixin, Base):
    """A reusable election/event blueprint, snapshotted from a real election
    or event the organizer owns at save time — categories/ticket tiers are
    captured as data here, not a live reference, so editing or deleting the
    source afterward never changes what the template applies. "Use this
    template" itself (pre-filling the create wizard's draft) is entirely
    client-side — see apply-template.tsx — this model only backs the
    save/list/delete side.

    A "system" template (the curated starter set Pollord ships to everyone)
    is deliberately NOT a row here — it's fixed, non-user-generated content
    that lives in the frontend, same as any other curated copy."""

    __tablename__ = "templates"

    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # See Election.org_id for why this is nullable — same personal-vs-org rule.
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.org_id"), nullable=True, index=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # "public" = every organizer on the platform can see and use this
    # template, not just the owner's own org — deliberately the one
    # cross-tenant-visible resource in the app (see the migration's RLS
    # policy docstring for how that's enforced).
    visibility: Mapped[str] = mapped_column(String(10), default="private", nullable=False)
    # [name, ...] snapshot of the source's Category names.
    categories: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    # [{name, price, description, available}, ...] snapshot of the source
    # event's TicketTypes. Always null for a kind="election" template.
    ticket_tiers: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    voting_type: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    vote_price_pesewas: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    creator: Mapped["User"] = relationship(lazy="select")
    organization: Mapped[Optional["Organization"]] = relationship(lazy="select")
