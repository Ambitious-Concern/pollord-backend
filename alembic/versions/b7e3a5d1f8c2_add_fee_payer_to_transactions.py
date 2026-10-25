"""add fee_payer to transactions and ticket_transactions

Revision ID: b7e3a5d1f8c2
Revises: a1c4f9e2d6b3
Create Date: 2026-09-18 00:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e3a5d1f8c2'
down_revision: Union[str, None] = 'a1c4f9e2d6b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Snapshotted from the parent election/event's fee_payer at initiation —
    # see the comment on the Transaction/TicketTransaction models. Every
    # existing (and future-by-default) row keeps meaning "commission comes
    # out of the payout," matching behavior before this column existed.
    op.add_column(
        'transactions',
        sa.Column(
            'fee_payer',
            sa.String(length=20),
            nullable=False,
            server_default='organizer',
        ),
    )
    op.add_column(
        'ticket_transactions',
        sa.Column(
            'fee_payer',
            sa.String(length=20),
            nullable=False,
            server_default='organizer',
        ),
    )


def downgrade() -> None:
    op.drop_column('ticket_transactions', 'fee_payer')
    op.drop_column('transactions', 'fee_payer')
