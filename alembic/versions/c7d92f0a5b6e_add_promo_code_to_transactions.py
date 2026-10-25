"""add promo code fields to transactions

Revision ID: c7d92f0a5b6e
Revises: b4f1a2c7e9d3
Create Date: 2026-09-16

Plain (no FK) promo_code_id columns, matching transactions/ticket_transactions'
existing no-FK convention (see app/models/transaction.py). vote_count on
Transaction lets verify-and-cast trust the count decided at initiation
instead of re-deriving it via amount // vote_price, which would silently
undercount once a promo code changes the price per vote.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c7d92f0a5b6e'
down_revision = 'b4f1a2c7e9d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('transactions', sa.Column('promo_code_id', sa.UUID(), nullable=True))
    op.add_column('transactions', sa.Column('discount_pesewas', sa.Integer(), nullable=True))
    op.add_column('transactions', sa.Column('vote_count', sa.Integer(), nullable=True))

    op.add_column('ticket_transactions', sa.Column('promo_code_id', sa.UUID(), nullable=True))
    op.add_column(
        'ticket_transactions',
        sa.Column('discount_amount', sa.Numeric(10, 2), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('ticket_transactions', 'discount_amount')
    op.drop_column('ticket_transactions', 'promo_code_id')

    op.drop_column('transactions', 'vote_count')
    op.drop_column('transactions', 'discount_pesewas')
    op.drop_column('transactions', 'promo_code_id')
