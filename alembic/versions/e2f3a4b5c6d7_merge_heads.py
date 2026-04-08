"""merge heads

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6, ec27a9809328
Create Date: 2026-04-07 00:01:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2f3a4b5c6d7'
down_revision: Union[str, None] = ('d1e2f3a4b5c6', 'ec27a9809328')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
