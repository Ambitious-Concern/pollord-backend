"""add ticket confirmation email tracking

Revision ID: c4a1f7e92b30
Revises: 21fbf74929c3
Create Date: 2026-08-21 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4a1f7e92b30'
down_revision: Union[str, None] = '21fbf74929c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows stay NULL — we genuinely don't know whether their ticket
    # email arrived, and pretending otherwise would hide the very purchases
    # an admin needs to find.
    op.add_column(
        'ticket_purchases',
        sa.Column('confirmation_email_status', sa.String(length=20), nullable=True),
    )
    op.add_column(
        'ticket_purchases',
        sa.Column(
            'confirmation_email_attempted_at',
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        'ticket_purchases',
        sa.Column('confirmation_email_to', sa.String(length=255), nullable=True),
    )
    # The admin console's default view is "everything that isn't sent", so
    # this column is filtered on nearly every request.
    op.create_index(
        'ix_ticket_purchases_confirmation_email_status',
        'ticket_purchases',
        ['confirmation_email_status'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_ticket_purchases_confirmation_email_status',
        table_name='ticket_purchases',
    )
    op.drop_column('ticket_purchases', 'confirmation_email_to')
    op.drop_column('ticket_purchases', 'confirmation_email_attempted_at')
    op.drop_column('ticket_purchases', 'confirmation_email_status')
