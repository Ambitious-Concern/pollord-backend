"""add origin to tickets

Revision ID: 92a7ebc441ac
Revises: 94ffa511732e
Create Date: 2026-09-16

Free-text "coming from" note captured at Front Desk walk-up entry.
"""
from alembic import op
import sqlalchemy as sa

revision = '92a7ebc441ac'
down_revision = '94ffa511732e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("origin", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("tickets", "origin")
