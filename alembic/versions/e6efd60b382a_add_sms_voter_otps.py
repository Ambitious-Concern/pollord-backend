"""add sms_voter_otps table

Revision ID: e6efd60b382a
Revises: a63775ddb057
Create Date: 2026-09-16

Premium+ SMS alternative to voter_otps' email channel — see
core/security.generate_sms_voter_hash and core/plans.py. Wired into the same
RLS category-child policy as voter_otps (a834750eb41e already created the
pollord_app_rls role and category_org_id() helper this reuses).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'e6efd60b382a'
down_revision = 'a63775ddb057'
branch_labels = None
depends_on = None

RLS_ROLE = "pollord_app_rls"
TABLE = "sms_voter_otps"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('category_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('phone', sa.String(length=20), nullable=False),
        sa.Column('otp_code', sa.String(length=6), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['category_id'], ['categories.category_id'], ondelete='CASCADE'),
    )
    op.create_index(f'ix_{TABLE}_category_id', TABLE, ['category_id'])

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {RLS_ROLE}")
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{TABLE} ON {TABLE}
        USING (
            category_org_id(category_id) IS NULL
            OR category_org_id(category_id) = ANY(current_org_ids())
        )
        WITH CHECK (
            category_org_id(category_id) IS NULL
            OR category_org_id(category_id) = ANY(current_org_ids())
        )
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{TABLE} ON {TABLE}")
    op.execute(f"ALTER TABLE {TABLE} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON {TABLE} FROM {RLS_ROLE}")

    op.drop_index(f'ix_{TABLE}_category_id', table_name=TABLE)
    op.drop_table(TABLE)
