"""add fee_payer to elections and events

Revision ID: a1c4f9e2d6b3
Revises: 77d61d87b60f
Create Date: 2026-09-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c4f9e2d6b3'
down_revision: Union[str, None] = '77d61d87b60f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default 'organizer': every existing election/event keeps the
    # commission coming out of the payout exactly as it does today — a
    # buyer-absorbed surcharge only applies once an organizer opts in.
    op.add_column(
        'elections',
        sa.Column(
            'fee_payer',
            sa.String(length=20),
            nullable=False,
            server_default='organizer',
        ),
    )
    op.add_column(
        'events',
        sa.Column(
            'fee_payer',
            sa.String(length=20),
            nullable=False,
            server_default='organizer',
        ),
    )


def downgrade() -> None:
    op.drop_column('events', 'fee_payer')
    op.drop_column('elections', 'fee_payer')
