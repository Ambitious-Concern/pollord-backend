"""add short_code to candidates

Revision ID: c5d6e7f8a9b0
Revises: b2c3d4e5f6a7
Create Date: 2026-04-08

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = 'c5d6e7f8a9b0'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = [c['name'] for c in inspector.get_columns('candidates')]

    if 'short_code' not in columns:
        op.add_column('candidates', sa.Column('short_code', sa.String(6), nullable=True))
        op.create_index('ix_candidates_short_code', 'candidates', ['short_code'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_candidates_short_code', table_name='candidates')
    op.drop_column('candidates', 'short_code')
