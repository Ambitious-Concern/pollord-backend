"""add users.onboarding_completed_at

Revision ID: a1c4e7f20b93
Revises: df904d0619c4
Create Date: 2026-10-24 21:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c4e7f20b93'
down_revision: Union[str, None] = 'df904d0619c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("onboarding_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # NULL means "has not seen the welcome tour", so every pre-existing
    # organizer would be shown one on their next login. Backfill them as
    # already done — the tour is for people arriving new, not for accounts
    # that have been running elections for months.
    op.execute("UPDATE users SET onboarding_completed_at = NOW()")


def downgrade() -> None:
    op.drop_column("users", "onboarding_completed_at")
