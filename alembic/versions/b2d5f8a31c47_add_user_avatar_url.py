"""add users.avatar_url

Revision ID: b2d5f8a31c47
Revises: a1c4e7f20b93
Create Date: 2026-10-24 22:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2d5f8a31c47'
down_revision: Union[str, None] = 'a1c4e7f20b93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable with no backfill: NULL is the "no picture yet" state the UI
    # already handles by rendering an initials monogram, so there is nothing
    # sensible to seed it with.
    op.add_column("users", sa.Column("avatar_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "avatar_url")
