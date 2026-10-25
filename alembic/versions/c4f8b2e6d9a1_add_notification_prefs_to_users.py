"""add notification_prefs to users

Revision ID: c4f8b2e6d9a1
Revises: b2e6a9d1f4c3
Create Date: 2026-09-19 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4f8b2e6d9a1'
down_revision: Union[str, None] = 'b2e6a9d1f4c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('notification_prefs', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'notification_prefs')
