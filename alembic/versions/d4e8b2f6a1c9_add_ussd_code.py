"""add ussd_code to elections and events

Revision ID: d4e8b2f6a1c9
Revises: c3f7a9b1e6d4
Create Date: 2026-09-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e8b2f6a1c9'
down_revision: Union[str, None] = 'c3f7a9b1e6d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('elections', sa.Column('ussd_code', sa.String(length=10), nullable=True))
    op.create_unique_constraint('uq_elections_ussd_code', 'elections', ['ussd_code'])
    op.create_index(op.f('ix_elections_ussd_code'), 'elections', ['ussd_code'], unique=False)

    op.add_column('events', sa.Column('ussd_code', sa.String(length=10), nullable=True))
    op.create_unique_constraint('uq_events_ussd_code', 'events', ['ussd_code'])
    op.create_index(op.f('ix_events_ussd_code'), 'events', ['ussd_code'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_events_ussd_code'), table_name='events')
    op.drop_constraint('uq_events_ussd_code', 'events', type_='unique')
    op.drop_column('events', 'ussd_code')

    op.drop_index(op.f('ix_elections_ussd_code'), table_name='elections')
    op.drop_constraint('uq_elections_ussd_code', 'elections', type_='unique')
    op.drop_column('elections', 'ussd_code')
