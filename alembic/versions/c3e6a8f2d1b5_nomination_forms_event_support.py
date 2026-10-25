"""nomination forms: add event_id, make election_id nullable

Revision ID: c3e6a8f2d1b5
Revises: b2d5f8a31c47
Create Date: 2026-10-25 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3e6a8f2d1b5"
down_revision: Union[str, None] = "b2d5f8a31c47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # nomination_forms: make election_id nullable, add event_id FK + check
    op.alter_column("nomination_forms", "election_id", nullable=True)
    op.add_column(
        "nomination_forms",
        sa.Column(
            "event_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("events.event_id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_nomination_forms_event_id", "nomination_forms", ["event_id"]
    )
    op.create_check_constraint(
        "nomination_forms_election_or_event",
        "nomination_forms",
        "(election_id IS NOT NULL) != (event_id IS NOT NULL)",
    )

    # nomination_submissions: make election_id nullable, add event_id FK + check
    op.alter_column("nomination_submissions", "election_id", nullable=True)
    op.add_column(
        "nomination_submissions",
        sa.Column(
            "event_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("events.event_id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_nomination_submissions_event_id", "nomination_submissions", ["event_id"]
    )
    op.create_check_constraint(
        "nomination_submissions_election_or_event",
        "nomination_submissions",
        "(election_id IS NOT NULL) != (event_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "nomination_submissions_election_or_event", "nomination_submissions"
    )
    op.drop_index("ix_nomination_submissions_event_id", "nomination_submissions")
    op.drop_column("nomination_submissions", "event_id")
    op.alter_column("nomination_submissions", "election_id", nullable=False)

    op.drop_constraint(
        "nomination_forms_election_or_event", "nomination_forms"
    )
    op.drop_index("ix_nomination_forms_event_id", "nomination_forms")
    op.drop_column("nomination_forms", "event_id")
    op.alter_column("nomination_forms", "election_id", nullable=False)
