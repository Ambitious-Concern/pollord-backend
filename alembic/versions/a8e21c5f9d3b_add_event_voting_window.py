"""add event voting window

Revision ID: a8e21c5f9d3b
Revises: f3c8d0a71b52
Create Date: 2026-08-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a8e21c5f9d3b'
down_revision: Union[str, None] = 'f3c8d0a71b52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Both nullable, both null by default: existing events keep falling back
    # to "own start time through end of that calendar day" for voting until
    # an organizer explicitly sets an independent window.
    op.add_column('events', sa.Column('voting_starts_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('events', sa.Column('voting_ends_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('events', 'voting_ends_at')
    op.drop_column('events', 'voting_starts_at')
