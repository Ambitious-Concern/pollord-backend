"""add brand_primary_color to organizations

Revision ID: ac17c44cd065
Revises: 641302cfb5cd
Create Date: 2026-09-15 20:37:30.851843

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ac17c44cd065'
down_revision: Union[str, None] = '641302cfb5cd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations", sa.Column("brand_primary_color", sa.String(7), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("organizations", "brand_primary_color")
