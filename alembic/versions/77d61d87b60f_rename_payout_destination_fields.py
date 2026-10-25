"""rename payout destination fields

Revision ID: 77d61d87b60f
Revises: a9b3c7d1e5f2
Create Date: 2026-09-17 21:45:42.443921

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '77d61d87b60f'
down_revision: Union[str, None] = 'a9b3c7d1e5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('payout_requests', 'mobile_network', new_column_name='destination_code')
    op.alter_column('payout_requests', 'mobile_number', new_column_name='destination_number')


def downgrade() -> None:
    op.alter_column('payout_requests', 'destination_code', new_column_name='mobile_network')
    op.alter_column('payout_requests', 'destination_number', new_column_name='mobile_number')
