"""add platform_settings table and make vote_price nullable

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-04-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision: str = 'c6d7e8f9a0b1'
down_revision: Union[str, None] = 'b5c6d7e8f9a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = inspector.get_table_names()

    # Create platform_settings table
    if 'platform_settings' not in tables:
        op.create_table(
            'platform_settings',
            sa.Column('key', sa.String(100), primary_key=True),
            sa.Column('value', sa.Text(), nullable=False),
            sa.Column('updated_by', sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        # Seed the default global vote price (100 pesewas = ₵1)
        op.execute(text("INSERT INTO platform_settings (key, value) VALUES ('vote_price', '100')"))

    # Make elections.vote_price nullable (was NOT NULL with server_default 100)
    elections_cols = {col['name']: col for col in inspector.get_columns('elections')}
    if 'vote_price' in elections_cols and not elections_cols['vote_price']['nullable']:
        op.alter_column('elections', 'vote_price', nullable=True, server_default=None)


def downgrade() -> None:
    op.alter_column('elections', 'vote_price', nullable=False, server_default='100')
    op.drop_table('platform_settings')
