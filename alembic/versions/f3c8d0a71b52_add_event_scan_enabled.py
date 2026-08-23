"""add event scan_enabled

Revision ID: f3c8d0a71b52
Revises: e7b2c91d4a08
Create Date: 2026-08-23 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3c8d0a71b52'
down_revision: Union[str, None] = 'e7b2c91d4a08'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default true: every existing event keeps scanning exactly as it
    # does now. Defaulting to false would silently disable check-in for live
    # events the moment this deploys.
    op.add_column(
        'events',
        sa.Column(
            'scan_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
    )


def downgrade() -> None:
    op.drop_column('events', 'scan_enabled')
