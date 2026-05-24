"""add count column to votes

Revision ID: a1b2c3d4e5f6_vc
Revises: f8a9b0c1d2e3
Create Date: 2026-04-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1b2c3d4e5f6_vc'
down_revision = 'f8a9b0c1d2e3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'votes',
        sa.Column('count', sa.Integer(), nullable=False, server_default='1'),
    )


def downgrade() -> None:
    op.drop_column('votes', 'count')
