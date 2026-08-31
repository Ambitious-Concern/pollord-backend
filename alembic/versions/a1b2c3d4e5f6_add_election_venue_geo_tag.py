"""add election venue, geo, and tag fields

Revision ID: a1b2c3d4e5f6
Revises: 32cf7876b67c
Create Date: 2026-08-30

Adds venue/latitude/longitude/tag to elections, mirroring the
location/category fields Event already has. All nullable — no backfill
needed, existing elections just have no venue set.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1b2c3d4e5f6'
down_revision = '32cf7876b67c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('elections', sa.Column('venue', sa.String(length=500), nullable=True))
    op.add_column('elections', sa.Column('latitude', sa.Float(), nullable=True))
    op.add_column('elections', sa.Column('longitude', sa.Float(), nullable=True))
    op.add_column('elections', sa.Column('tag', sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column('elections', 'tag')
    op.drop_column('elections', 'longitude')
    op.drop_column('elections', 'latitude')
    op.drop_column('elections', 'venue')
