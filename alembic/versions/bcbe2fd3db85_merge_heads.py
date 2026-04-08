"""merge heads

Revision ID: bcbe2fd3db85
Revises: 2dfdc9e7e951, c4d5e6f7a8b9
Create Date: 2026-03-17 04:13:25.559160

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bcbe2fd3db85'
down_revision: Union[str, None] = ('2dfdc9e7e951', 'c4d5e6f7a8b9')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
