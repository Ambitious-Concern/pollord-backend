"""add waitlist_subscribers

Revision ID: 62c1756c2dee
Revises: 9b63eff64836
Create Date: 2026-08-06 19:52:25.587684

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '62c1756c2dee'
down_revision: Union[str, None] = '9b63eff64836'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'waitlist_subscribers',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_waitlist_subscribers_email'), 'waitlist_subscribers', ['email'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_waitlist_subscribers_email'), table_name='waitlist_subscribers')
    op.drop_table('waitlist_subscribers')
