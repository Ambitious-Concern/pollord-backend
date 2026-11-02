"""Rename plan values: standard→voting, premium→ticketing

Elections now default to the 'voting' plan; events to 'ticketing'.
Custom stays unchanged. See app/core/plans.py for the new constants.

Revision ID: c1d2e3f4a5b6
Revises: b9e3f1a7c2d6
Create Date: 2026-10-27
"""
from typing import Union

from alembic import op

revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, None] = 'b9e3f1a7c2d6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Elections: standard → voting, premium → voting (both merge into one product tier)
    op.execute("UPDATE elections SET plan = 'voting'    WHERE plan = 'standard'")
    op.execute("UPDATE elections SET plan = 'voting'    WHERE plan = 'premium'")

    # Events: standard → ticketing, premium → ticketing
    op.execute("UPDATE events    SET plan = 'ticketing' WHERE plan = 'standard'")
    op.execute("UPDATE events    SET plan = 'ticketing' WHERE plan = 'premium'")

    # Also update the server defaults on both columns
    op.alter_column('elections', 'plan', server_default='voting')
    op.alter_column('events',    'plan', server_default='ticketing')


def downgrade() -> None:
    # Best-effort rollback: map back to standard (custom stays unchanged)
    op.execute("UPDATE elections SET plan = 'standard' WHERE plan = 'voting'")
    op.execute("UPDATE events    SET plan = 'standard' WHERE plan = 'ticketing'")

    op.alter_column('elections', 'plan', server_default='standard')
    op.alter_column('events',    'plan', server_default='standard')
