"""create org_activity_logs

Revision ID: 583da04a7401
Revises: 0007585d6050
Create Date: 2026-09-16

Team-facing activity feed, scoped by a real org_id column — distinct from
audit_logs (the platform-wide SuperAdmin console log, left untouched).
Wired into RLS same as the other genuinely org-scoped tables; unlike
elections/events there's no personal/org-less state here (org_id is
NOT NULL), so the policy has no "IS NULL" branch.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '583da04a7401'
down_revision = '0007585d6050'
branch_labels = None
depends_on = None

RLS_ROLE = "pollord_app_rls"
TABLE = "org_activity_logs"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column('log_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('org_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('category', sa.String(length=20), nullable=False),
        sa.Column('verb', sa.String(length=100), nullable=False),
        sa.Column('target_name', sa.String(length=255), nullable=False),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('ip_address', sa.String(length=45), nullable=True),
        sa.Column('user_agent', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.org_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id']),
    )
    op.create_index('idx_org_activity_org_created', TABLE, ['org_id', 'created_at'])
    op.create_index('ix_org_activity_logs_category', TABLE, ['category'])

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {RLS_ROLE}")
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{TABLE} ON {TABLE}
        USING (org_id = ANY(current_org_ids()))
        WITH CHECK (org_id = ANY(current_org_ids()))
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{TABLE} ON {TABLE}")
    op.execute(f"ALTER TABLE {TABLE} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON {TABLE} FROM {RLS_ROLE}")

    op.drop_index('ix_org_activity_logs_category', table_name=TABLE)
    op.drop_index('idx_org_activity_org_created', table_name=TABLE)
    op.drop_table(TABLE)
