"""add vote_price to elections

Revision ID: b5c6d7e8f9a0
Revises: a1b2c3d4e5f6_vc
Create Date: 2026-04-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = 'b5c6d7e8f9a0'
down_revision: Union[str, None] = 'a1b2c3d4e5f6_vc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = [col['name'] for col in inspector.get_columns('elections')]
    if 'vote_price' not in columns:
        op.add_column(
            'elections',
            sa.Column('vote_price', sa.Integer(), nullable=False, server_default='100'),
        )


def downgrade() -> None:
    op.drop_column('elections', 'vote_price')
