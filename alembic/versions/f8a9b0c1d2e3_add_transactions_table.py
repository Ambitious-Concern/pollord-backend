"""add transactions table

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-04-08

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'f8a9b0c1d2e3'
down_revision: Union[str, None] = 'e7f8a9b0c1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'transactions',
        sa.Column('transaction_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('reference', sa.String(100), nullable=False),
        sa.Column('election_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('voter_hash', sa.String(64), nullable=False),
        sa.Column('email', sa.String(255), nullable=True),
        sa.Column('candidate_ids', postgresql.JSONB(), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(10), nullable=False, server_default='GHS'),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('paystack_response', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('transaction_id'),
        sa.UniqueConstraint('reference', name='uq_transaction_reference'),
    )
    op.create_index('ix_transactions_reference', 'transactions', ['reference'])
    op.create_index('ix_transactions_election_id', 'transactions', ['election_id'])
    op.create_index('ix_transactions_status', 'transactions', ['status'])


def downgrade() -> None:
    op.drop_index('ix_transactions_status', table_name='transactions')
    op.drop_index('ix_transactions_election_id', table_name='transactions')
    op.drop_index('ix_transactions_reference', table_name='transactions')
    op.drop_table('transactions')
