"""add ip_address/user_agent to refresh_tokens

Revision ID: f3c9e1a7b5d2
Revises: e8b2d5f4a1c9
Create Date: 2026-09-19 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3c9e1a7b5d2'
down_revision: Union[str, None] = 'e8b2d5f4a1c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('refresh_tokens', sa.Column('ip_address', sa.String(length=45), nullable=True))
    op.add_column('refresh_tokens', sa.Column('user_agent', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('refresh_tokens', 'user_agent')
    op.drop_column('refresh_tokens', 'ip_address')
