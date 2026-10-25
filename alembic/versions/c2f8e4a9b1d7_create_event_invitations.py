"""create event_invitations

Revision ID: c2f8e4a9b1d7
Revises: b7e3a5d1f8c2
Create Date: 2026-09-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c2f8e4a9b1d7'
down_revision: Union[str, None] = 'b7e3a5d1f8c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'event_invitations',
        sa.Column('invitation_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'event_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('events.event_id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('token', sa.String(length=128), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column(
            'invited_by',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('users.user_id'),
            nullable=True,
        ),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_event_invitations_event_id', 'event_invitations', ['event_id'])
    op.create_index('ix_event_invitations_token', 'event_invitations', ['token'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_event_invitations_token', table_name='event_invitations')
    op.drop_index('ix_event_invitations_event_id', table_name='event_invitations')
    op.drop_table('event_invitations')
