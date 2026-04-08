"""add allow_revoting to elections

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-04-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = 'e7f8a9b0c1d2'
down_revision: Union[str, None] = 'd6e7f8a9b0c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = [col['name'] for col in inspector.get_columns('elections')]
    if 'allow_revoting' not in columns:
        op.add_column(
            'elections',
            sa.Column('allow_revoting', sa.Boolean(), nullable=False, server_default='false'),
        )


def downgrade() -> None:
    op.drop_column('elections', 'allow_revoting')
