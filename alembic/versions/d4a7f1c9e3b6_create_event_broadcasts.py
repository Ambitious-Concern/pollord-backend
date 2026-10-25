"""create event_broadcasts

Revision ID: d4a7f1c9e3b6
Revises: c2f8e4a9b1d7
Create Date: 2026-09-19 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd4a7f1c9e3b6'
down_revision: Union[str, None] = 'c2f8e4a9b1d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'event_broadcasts',
        sa.Column('broadcast_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'event_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('events.event_id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column(
            'sent_by',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.user_id'),
            nullable=False,
        ),
        sa.Column('subject', sa.String(length=255), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('recipient_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('sent_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('failed_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='sending'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_event_broadcasts_event_id', 'event_broadcasts', ['event_id'])


def downgrade() -> None:
    op.drop_index('ix_event_broadcasts_event_id', table_name='event_broadcasts')
    op.drop_table('event_broadcasts')
