"""create nomination tables

Revision ID: 94ffa511732e
Revises: 083193aa4cc6
Create Date: 2026-09-16

NominationForm (+ its custom fields) and NominationSubmission — the
nomination-compiling pipeline: a shareable form, individual submissions
grouped by (category, nominee_name) for review, and approval turns a group
into a real Candidate (see NominationService).

RLS: nomination_forms and nomination_submissions are election-child tables
(same pattern as eligible_voters), scoped via the existing election_org_id()
helper. nomination_form_fields is a 2-hop child (via form_id), so this adds
one new SECURITY DEFINER helper, nomination_form_org_id(), mirroring
category_org_id()'s existing pattern.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '94ffa511732e'
down_revision = '083193aa4cc6'
branch_labels = None
depends_on = None

RLS_ROLE = "pollord_app_rls"


def upgrade() -> None:
    op.create_table(
        'nomination_forms',
        sa.Column('form_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('election_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('visibility', sa.String(length=10), nullable=False, server_default='public'),
        sa.Column('slug', sa.String(length=255), nullable=False),
        sa.Column('created_by', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), onupdate=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(['election_id'], ['elections.election_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['users.user_id']),
        sa.UniqueConstraint('slug', name='uq_nomination_forms_slug'),
    )
    op.create_index('ix_nomination_forms_election_id', 'nomination_forms', ['election_id'])
    op.create_index('ix_nomination_forms_slug', 'nomination_forms', ['slug'])

    op.create_table(
        'nomination_form_fields',
        sa.Column('field_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('form_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('field_type', sa.String(length=20), nullable=False),
        sa.Column('label', sa.String(length=255), nullable=False),
        sa.Column('placeholder', sa.String(length=255), nullable=True),
        sa.Column('required', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('options', postgresql.JSONB(), nullable=True),
        sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'),
        sa.ForeignKeyConstraint(['form_id'], ['nomination_forms.form_id'], ondelete='CASCADE'),
    )
    op.create_index('ix_nomination_form_fields_form_id', 'nomination_form_fields', ['form_id'])

    op.create_table(
        'nomination_submissions',
        sa.Column('submission_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('form_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('election_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('category_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('nominee_name', sa.String(length=255), nullable=False),
        sa.Column('source', sa.String(length=10), nullable=False),
        sa.Column('answers', postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('submitter_ip', sa.String(length=45), nullable=True),
        sa.Column('submitter_user_agent', sa.Text(), nullable=True),
        sa.Column('submitted_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['form_id'], ['nomination_forms.form_id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['election_id'], ['elections.election_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['category_id'], ['categories.category_id'], ondelete='CASCADE'),
    )
    op.create_index('ix_nomination_submissions_election_id', 'nomination_submissions', ['election_id'])
    op.create_index('ix_nomination_submissions_category_id', 'nomination_submissions', ['category_id'])
    op.create_index('ix_nomination_submissions_status', 'nomination_submissions', ['status'])

    # --- RLS ---------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION nomination_form_org_id(p_form_id uuid) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
            SELECT election_org_id(nf.election_id)
            FROM nomination_forms nf WHERE nf.form_id = p_form_id
        $$;
        """
    )

    for table in ("nomination_forms", "nomination_submissions"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {RLS_ROLE}")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation_{table} ON {table}
            USING (
                election_org_id(election_id) IS NULL
                OR election_org_id(election_id) = ANY(current_org_ids())
            )
            WITH CHECK (
                election_org_id(election_id) IS NULL
                OR election_org_id(election_id) = ANY(current_org_ids())
            )
            """
        )

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON nomination_form_fields TO {RLS_ROLE}")
    op.execute("ALTER TABLE nomination_form_fields ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE nomination_form_fields FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_nomination_form_fields ON nomination_form_fields
        USING (
            nomination_form_org_id(form_id) IS NULL
            OR nomination_form_org_id(form_id) = ANY(current_org_ids())
        )
        WITH CHECK (
            nomination_form_org_id(form_id) IS NULL
            OR nomination_form_org_id(form_id) = ANY(current_org_ids())
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation_nomination_form_fields ON nomination_form_fields")
    op.execute("ALTER TABLE nomination_form_fields NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE nomination_form_fields DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON nomination_form_fields FROM {RLS_ROLE}")

    for table in ("nomination_submissions", "nomination_forms"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON {table} FROM {RLS_ROLE}")

    op.execute("DROP FUNCTION IF EXISTS nomination_form_org_id(uuid)")

    op.drop_index('ix_nomination_submissions_status', table_name='nomination_submissions')
    op.drop_index('ix_nomination_submissions_category_id', table_name='nomination_submissions')
    op.drop_index('ix_nomination_submissions_election_id', table_name='nomination_submissions')
    op.drop_table('nomination_submissions')

    op.drop_index('ix_nomination_form_fields_form_id', table_name='nomination_form_fields')
    op.drop_table('nomination_form_fields')

    op.drop_index('ix_nomination_forms_slug', table_name='nomination_forms')
    op.drop_index('ix_nomination_forms_election_id', table_name='nomination_forms')
    op.drop_table('nomination_forms')
