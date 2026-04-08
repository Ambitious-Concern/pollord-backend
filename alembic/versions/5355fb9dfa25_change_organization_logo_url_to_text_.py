"""change organization logo_url to text type

Revision ID: 5355fb9dfa25
Revises: bcbe2fd3db85
Create Date: 2026-03-17 04:15:38.255009

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5355fb9dfa25'
down_revision: Union[str, None] = 'bcbe2fd3db85'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('organizations', 'logo_url',
               existing_type=sa.VARCHAR(length=1000),
               type_=sa.Text(),
               existing_nullable=True)


def downgrade() -> None:
    op.alter_column('organizations', 'logo_url',
               existing_type=sa.Text(),
               type_=sa.VARCHAR(length=1000),
               existing_nullable=True)
