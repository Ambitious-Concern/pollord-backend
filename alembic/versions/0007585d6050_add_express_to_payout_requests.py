"""add express to payout_requests

Revision ID: 0007585d6050
Revises: e6efd60b382a
Create Date: 2026-09-16

Premium+ perk — see core/plans.py and PayoutService.request_payout.
"""
from alembic import op
import sqlalchemy as sa

revision = '0007585d6050'
down_revision = 'e6efd60b382a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payout_requests",
        sa.Column("express", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("payout_requests", "express")
