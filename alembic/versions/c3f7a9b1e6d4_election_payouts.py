"""allow payout requests for elections

Revision ID: c3f7a9b1e6d4
Revises: a8e21c5f9d3b
Create Date: 2026-08-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = 'c3f7a9b1e6d4'
down_revision: Union[str, None] = 'a8e21c5f9d3b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('payout_requests', 'event_id', nullable=True)
    op.add_column(
        'payout_requests',
        sa.Column('election_id', UUID(as_uuid=True), sa.ForeignKey('elections.election_id'), nullable=True),
    )
    op.create_index(
        op.f('ix_payout_requests_election_id'), 'payout_requests', ['election_id'], unique=False
    )
    op.create_check_constraint(
        'ck_payout_requests_exactly_one_parent',
        'payout_requests',
        '(event_id IS NOT NULL)::int + (election_id IS NOT NULL)::int = 1',
    )


def downgrade() -> None:
    op.drop_constraint('ck_payout_requests_exactly_one_parent', 'payout_requests', type_='check')
    op.drop_index(op.f('ix_payout_requests_election_id'), table_name='payout_requests')
    op.drop_column('payout_requests', 'election_id')
    op.alter_column('payout_requests', 'event_id', nullable=False)
