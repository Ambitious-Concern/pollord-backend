"""add candidate access otps

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-04-08

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if 'candidate_access_otps' not in inspector.get_table_names():
        op.create_table(
            'candidate_access_otps',
            sa.Column('id', UUID(as_uuid=True), primary_key=True),
            sa.Column('election_id', UUID(as_uuid=True),
                      sa.ForeignKey('elections.election_id', ondelete='CASCADE'), nullable=False),
            sa.Column('candidate_id', UUID(as_uuid=True),
                      sa.ForeignKey('candidates.candidate_id', ondelete='CASCADE'), nullable=False),
            sa.Column('email', sa.String(255), nullable=False),
            sa.Column('otp_code', sa.String(6), nullable=False),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('used', sa.Boolean(), nullable=False, server_default='false'),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    op.drop_table('candidate_access_otps')
