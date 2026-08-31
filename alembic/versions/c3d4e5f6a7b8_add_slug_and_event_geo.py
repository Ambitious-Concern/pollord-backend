"""add slug to elections/events, lat/lng to events

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-30

v2's public pages route by slug (/v2/listings/:slug/...) instead of UUID.
Adds a unique, backfilled `slug` column to both elections and events, plus
latitude/longitude on events (elections already have them).
"""
import re
import secrets

from alembic import op
import sqlalchemy as sa

revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def _slugify(title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "listing"
    return f"{base}-{secrets.token_hex(2)}"


def _backfill_slugs(conn, table_name: str, id_column: str) -> None:
    table = sa.table(
        table_name,
        sa.column(id_column, sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.column("title", sa.String()),
        sa.column("slug", sa.String()),
    )
    rows = conn.execute(sa.select(table.c[id_column], table.c.title)).fetchall()
    used: set[str] = set()
    for row_id, title in rows:
        slug = _slugify(title or "listing")
        while slug in used:
            slug = _slugify(title or "listing")
        used.add(slug)
        conn.execute(
            table.update().where(table.c[id_column] == row_id).values(slug=slug)
        )


def upgrade() -> None:
    op.add_column('elections', sa.Column('slug', sa.String(length=255), nullable=True))
    op.add_column('events', sa.Column('slug', sa.String(length=255), nullable=True))
    op.add_column('events', sa.Column('latitude', sa.Float(), nullable=True))
    op.add_column('events', sa.Column('longitude', sa.Float(), nullable=True))

    conn = op.get_bind()
    _backfill_slugs(conn, 'elections', 'election_id')
    _backfill_slugs(conn, 'events', 'event_id')

    op.create_index('ix_elections_slug', 'elections', ['slug'], unique=True)
    op.create_index('ix_events_slug', 'events', ['slug'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_events_slug', table_name='events')
    op.drop_index('ix_elections_slug', table_name='elections')
    op.drop_column('events', 'longitude')
    op.drop_column('events', 'latitude')
    op.drop_column('events', 'slug')
    op.drop_column('elections', 'slug')
