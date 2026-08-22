"""add event show_ticket_counts

Revision ID: e7b2c91d4a08
Revises: c4a1f7e92b30
Create Date: 2026-08-22 04:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7b2c91d4a08'
down_revision: Union[str, None] = 'c4a1f7e92b30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default true so existing events keep showing counts exactly as
    # they do now — turning this on for everyone would be a silent change to
    # every live event page.
    op.add_column(
        'events',
        sa.Column(
            'show_ticket_counts',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
    )


def downgrade() -> None:
    op.drop_column('events', 'show_ticket_counts')
