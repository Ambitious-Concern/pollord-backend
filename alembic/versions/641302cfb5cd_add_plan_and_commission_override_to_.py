"""add plan and commission override to elections and events

Revision ID: 641302cfb5cd
Revises: a834750eb41e
Create Date: 2026-09-15 19:51:45.442064

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '641302cfb5cd'
down_revision: Union[str, None] = 'a834750eb41e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("elections", "events"):
        op.add_column(
            table,
            sa.Column("plan", sa.String(20), nullable=False, server_default="standard"),
        )
        op.add_column(
            table, sa.Column("commission_rate_override_bps", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    for table in ("elections", "events"):
        op.drop_column(table, "commission_rate_override_bps")
        op.drop_column(table, "plan")
