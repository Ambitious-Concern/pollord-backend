"""create notifications

Revision ID: a9b3c7d1e5f2
Revises: f3a7c2e9b8d4
Create Date: 2026-09-17

Notification backs the dashboard's bell/panel — a per-user feed distinct
from OrgActivityLog (a shared org-wide audit trail). Not part of the RLS
tenant table set: scoped by exact user_id match at the query level, same
treatment as refresh_tokens.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a9b3c7d1e5f2'
down_revision = 'f3a7c2e9b8d4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'notifications',
        sa.Column('notification_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('org_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('type', sa.String(length=20), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('description', sa.String(length=500), nullable=False),
        sa.Column('link', sa.String(length=255), nullable=True),
        sa.Column('is_read', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.org_id'], ondelete='CASCADE'),
    )
    op.create_index(
        'idx_notifications_user_created', 'notifications', ['user_id', 'created_at']
    )


def downgrade() -> None:
    op.drop_index('idx_notifications_user_created', table_name='notifications')
    op.drop_table('notifications')
