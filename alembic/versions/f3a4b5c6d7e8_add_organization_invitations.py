"""add organization invitations

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-04-08

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

revision = 'f3a4b5c6d7e8'
down_revision = 'e2f3a4b5c6d7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = inspector.get_table_names()

    if 'organization_invitations' not in existing_tables:
        op.create_table(
            'organization_invitations',
            sa.Column('invitation_id', UUID(as_uuid=True), primary_key=True),
            sa.Column('org_id', UUID(as_uuid=True),
                      sa.ForeignKey('organizations.org_id', ondelete='CASCADE'), nullable=False),
            sa.Column('email', sa.String(255), nullable=False),
            sa.Column('role', sa.String(30), nullable=False, server_default='member'),
            sa.Column('token', sa.String(128), unique=True, nullable=False),
            sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
            sa.Column('invited_by', UUID(as_uuid=True),
                      sa.ForeignKey('users.user_id'), nullable=True),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    # Create indexes only if they don't already exist
    existing_indexes = {
        idx['name']
        for idx in inspector.get_indexes('organization_invitations')
    } if 'organization_invitations' in existing_tables else set()

    if 'ix_org_invitations_token' not in existing_indexes:
        op.create_index('ix_org_invitations_token', 'organization_invitations', ['token'])
    if 'ix_org_invitations_email' not in existing_indexes:
        op.create_index('ix_org_invitations_email', 'organization_invitations', ['org_id', 'email'])


def downgrade() -> None:
    op.drop_table('organization_invitations')
