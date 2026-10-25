"""create report export jobs

Revision ID: f3a7c2e9b8d4
Revises: e2f8b9a4d1c6
Create Date: 2026-09-17

ReportExportJob backs backend-generated CSV/Excel/PDF report exports run as
a background Celery task. The (requested_by, idempotency_key) unique
constraint is the actual "prevent double clicks" mechanism — a duplicate
submit with the same key hits the constraint's covering index on lookup
before ever creating a second row.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'f3a7c2e9b8d4'
down_revision = 'e2f8b9a4d1c6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'report_export_jobs',
        sa.Column('job_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('requested_by', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('report_type', sa.String(length=20), nullable=False),
        sa.Column('format', sa.String(length=10), nullable=False),
        sa.Column('params', postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('idempotency_key', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('download_url', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), onupdate=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(['requested_by'], ['users.user_id']),
        sa.UniqueConstraint('requested_by', 'idempotency_key', name='uq_report_export_idempotency'),
    )
    op.create_index('ix_report_export_jobs_requested_by', 'report_export_jobs', ['requested_by'])


def downgrade() -> None:
    op.drop_index('ix_report_export_jobs_requested_by', table_name='report_export_jobs')
    op.drop_table('report_export_jobs')
