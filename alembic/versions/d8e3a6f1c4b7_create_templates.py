"""create templates

Revision ID: d8e3a6f1c4b7
Revises: c7d92f0a5b6e
Create Date: 2026-09-17

Template — a reusable election/event blueprint an organizer saves from a
real election/event they own, snapshotting its categories/ticket tiers.

RLS: templates is the first genuinely cross-tenant-visible resource in the
app. Every other table's policy reads "parent org is NULL, or parent org is
one of mine" — this one adds a third branch, "or it's marked public",
because SaveTemplateModal's own copy promises "other organizers on Pollord
can find and use this template too." A private template still follows the
usual org-or-personal rule; only visibility='public' rows cross the
boundary. The same predicate also governs WITH CHECK, so a caller can't
insert/update a private-looking row under someone else's org_id — only
truly public rows bypass the org check, in either direction.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd8e3a6f1c4b7'
down_revision = 'c7d92f0a5b6e'
branch_labels = None
depends_on = None

RLS_ROLE = "pollord_app_rls"


def upgrade() -> None:
    op.create_table(
        'templates',
        sa.Column('template_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('org_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_by', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('visibility', sa.String(length=10), nullable=False, server_default='private'),
        sa.Column('categories', postgresql.JSONB(), nullable=True),
        sa.Column('ticket_tiers', postgresql.JSONB(), nullable=True),
        sa.Column('voting_type', sa.String(length=10), nullable=True),
        sa.Column('vote_price_pesewas', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), onupdate=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.org_id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.user_id']),
    )
    op.create_index('ix_templates_org_id', 'templates', ['org_id'])

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON templates TO {RLS_ROLE}")
    op.execute("ALTER TABLE templates ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE templates FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_templates ON templates
        USING (
            visibility = 'public'
            OR org_id IS NULL
            OR org_id = ANY(current_org_ids())
        )
        WITH CHECK (
            visibility = 'public'
            OR org_id IS NULL
            OR org_id = ANY(current_org_ids())
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_templates ON templates")
    op.execute("ALTER TABLE templates NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE templates DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON templates FROM {RLS_ROLE}")

    op.drop_index('ix_templates_org_id', table_name='templates')
    op.drop_table('templates')
