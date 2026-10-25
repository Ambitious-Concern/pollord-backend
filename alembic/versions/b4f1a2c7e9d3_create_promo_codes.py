"""create promo codes

Revision ID: b4f1a2c7e9d3
Revises: 92a7ebc441ac
Create Date: 2026-09-16

PromoCode (+ its scope join table) and PromoCodeRedemption — a buyer-entered
discount code for ticket purchases and paid votes.

RLS: promo_codes is a ROOT_TABLES-style table (own nullable org_id, same
"NULL or one of mine" rule as elections/events). promo_code_scopes and
promo_code_redemptions are 2-hop children (via promo_code_id), so this adds
one new SECURITY DEFINER helper, promo_code_org_id(), mirroring
nomination_form_org_id()'s existing pattern.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'b4f1a2c7e9d3'
down_revision = '92a7ebc441ac'
branch_labels = None
depends_on = None

RLS_ROLE = "pollord_app_rls"


def upgrade() -> None:
    op.create_table(
        'promo_codes',
        sa.Column('promo_code_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('org_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_by', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('code', sa.String(length=30), nullable=False),
        sa.Column('discount_type', sa.String(length=20), nullable=False),
        sa.Column('discount_value', sa.Numeric(10, 2), nullable=False),
        sa.Column('usage_limit', sa.Integer(), nullable=True),
        sa.Column('per_person_limit', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('valid_from', sa.Date(), nullable=False),
        sa.Column('valid_until', sa.Date(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), onupdate=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.org_id']),
        sa.ForeignKeyConstraint(['created_by'], ['users.user_id']),
        sa.UniqueConstraint('code', name='uq_promo_codes_code'),
    )
    op.create_index('ix_promo_codes_org_id', 'promo_codes', ['org_id'])
    op.create_index('ix_promo_codes_code', 'promo_codes', ['code'])

    op.create_table(
        'promo_code_scopes',
        sa.Column('scope_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('promo_code_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('election_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('event_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            '(election_id IS NOT NULL) != (event_id IS NOT NULL)',
            name='ck_promo_scope_exactly_one_parent',
        ),
        sa.ForeignKeyConstraint(['promo_code_id'], ['promo_codes.promo_code_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['election_id'], ['elections.election_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['event_id'], ['events.event_id'], ondelete='CASCADE'),
    )
    op.create_index('ix_promo_code_scopes_promo_code_id', 'promo_code_scopes', ['promo_code_id'])

    op.create_table(
        'promo_code_redemptions',
        sa.Column('redemption_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('promo_code_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('channel', sa.String(length=10), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('discount_amount', sa.Numeric(10, 2), nullable=False),
        sa.Column('redeemed_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['promo_code_id'], ['promo_codes.promo_code_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id']),
    )
    op.create_index('ix_promo_code_redemptions_promo_code_id', 'promo_code_redemptions', ['promo_code_id'])
    op.create_index('ix_promo_code_redemptions_email', 'promo_code_redemptions', ['email'])

    # --- RLS ---------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION promo_code_org_id(p_promo_code_id uuid) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
            SELECT org_id FROM promo_codes WHERE promo_code_id = p_promo_code_id
        $$;
        """
    )

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON promo_codes TO {RLS_ROLE}")
    op.execute("ALTER TABLE promo_codes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE promo_codes FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_promo_codes ON promo_codes
        USING (org_id IS NULL OR org_id = ANY(current_org_ids()))
        WITH CHECK (org_id IS NULL OR org_id = ANY(current_org_ids()))
        """
    )

    for table in ("promo_code_scopes", "promo_code_redemptions"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {RLS_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation_{table} ON {table}
            USING (
                promo_code_org_id(promo_code_id) IS NULL
                OR promo_code_org_id(promo_code_id) = ANY(current_org_ids())
            )
            WITH CHECK (
                promo_code_org_id(promo_code_id) IS NULL
                OR promo_code_org_id(promo_code_id) = ANY(current_org_ids())
            )
            """
        )


def downgrade() -> None:
    for table in ("promo_code_redemptions", "promo_code_scopes"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON {table} FROM {RLS_ROLE}")

    op.execute("DROP POLICY IF EXISTS tenant_isolation_promo_codes ON promo_codes")
    op.execute("ALTER TABLE promo_codes NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE promo_codes DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON promo_codes FROM {RLS_ROLE}")

    op.execute("DROP FUNCTION IF EXISTS promo_code_org_id(uuid)")

    op.drop_index('ix_promo_code_redemptions_email', table_name='promo_code_redemptions')
    op.drop_index('ix_promo_code_redemptions_promo_code_id', table_name='promo_code_redemptions')
    op.drop_table('promo_code_redemptions')

    op.drop_index('ix_promo_code_scopes_promo_code_id', table_name='promo_code_scopes')
    op.drop_table('promo_code_scopes')

    op.drop_index('ix_promo_codes_code', table_name='promo_codes')
    op.drop_index('ix_promo_codes_org_id', table_name='promo_codes')
    op.drop_table('promo_codes')
