"""add rejection_reason to organizations

Revision ID: b9e3f1a7c2d6
Revises: a63775ddb057
Create Date: 2026-10-27 00:00:00.000000

"""
from typing import Union
from alembic import op
import sqlalchemy as sa


revision: str = 'b9e3f1a7c2d6'
down_revision: Union[str, None] = 'c3e6a8f2d1b5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'organizations',
        sa.Column('rejection_reason', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('organizations', 'rejection_reason')
