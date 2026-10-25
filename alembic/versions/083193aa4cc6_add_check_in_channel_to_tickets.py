"""add check_in_channel to tickets

Revision ID: 083193aa4cc6
Revises: 583da04a7401
Create Date: 2026-09-16

Distinguishes how a ticket was checked in (scan vs an organizer typing the
code in) — scanned_by only recorded who, not how.
"""
from alembic import op
import sqlalchemy as sa

revision = '083193aa4cc6'
down_revision = '583da04a7401'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tickets", sa.Column("check_in_channel", sa.String(20), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("tickets", "check_in_channel")
