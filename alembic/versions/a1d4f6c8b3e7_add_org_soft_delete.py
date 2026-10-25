"""add is_active/deactivated_at to organizations for soft-delete

Revision ID: a1d4f6c8b3e7
Revises: f3c9e1a7b5d2
Create Date: 2026-09-19 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1d4f6c8b3e7'
down_revision: Union[str, None] = 'f3c9e1a7b5d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'organizations',
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        'organizations',
        sa.Column('deactivated_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('organizations', 'deactivated_at')
    op.drop_column('organizations', 'is_active')
