"""add org_id to elections and events

Revision ID: 76eb6adeab0b
Revises: eca0d327674c
Create Date: 2026-09-15 13:57:45.344593

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '76eb6adeab0b'
down_revision: Union[str, None] = 'eca0d327674c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable — a creator with no organization runs their election/event
    # personally, which is a permanent state, not a gap to backfill (see the
    # next migration). Root of the RLS org-scoping work: everything else
    # (categories, votes, tickets, payouts, ...) reaches its owning org by
    # joining back to one of these two columns.
    op.add_column(
        'elections',
        sa.Column('org_id', sa.UUID(), nullable=True),
    )
    op.create_index('ix_elections_org_id', 'elections', ['org_id'])
    op.create_foreign_key(
        'fk_elections_org_id_organizations',
        'elections', 'organizations',
        ['org_id'], ['org_id'],
    )

    op.add_column(
        'events',
        sa.Column('org_id', sa.UUID(), nullable=True),
    )
    op.create_index('ix_events_org_id', 'events', ['org_id'])
    op.create_foreign_key(
        'fk_events_org_id_organizations',
        'events', 'organizations',
        ['org_id'], ['org_id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_events_org_id_organizations', 'events', type_='foreignkey')
    op.drop_index('ix_events_org_id', 'events')
    op.drop_column('events', 'org_id')

    op.drop_constraint('fk_elections_org_id_organizations', 'elections', type_='foreignkey')
    op.drop_index('ix_elections_org_id', 'elections')
    op.drop_column('elections', 'org_id')
