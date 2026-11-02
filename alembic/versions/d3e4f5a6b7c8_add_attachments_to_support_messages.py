"""add attachment fields to support_messages

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-10-28
"""
from alembic import op
import sqlalchemy as sa

revision = 'd3e4f5a6b7c8'
down_revision = 'c2d3e4f5a6b7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('support_messages', sa.Column('attachment_url', sa.String(2048), nullable=True))
    op.add_column('support_messages', sa.Column('attachment_name', sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column('support_messages', 'attachment_name')
    op.drop_column('support_messages', 'attachment_url')
