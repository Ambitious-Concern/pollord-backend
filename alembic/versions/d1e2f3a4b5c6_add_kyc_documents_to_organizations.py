"""add kyc documents to organizations

Revision ID: d1e2f3a4b5c6
Revises: bcbe2fd3db85
Create Date: 2026-04-07 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, None] = 'bcbe2fd3db85'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('organizations', sa.Column('kyc_document_front', sa.Text(), nullable=True))
    op.add_column('organizations', sa.Column('kyc_document_back', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('organizations', 'kyc_document_back')
    op.drop_column('organizations', 'kyc_document_front')
