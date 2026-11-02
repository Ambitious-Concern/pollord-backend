"""create backup records

Revision ID: d4e5f6a7b8c9
Revises: c1d2e3f4a5b6
Create Date: 2026-10-28

BackupRecord tracks every pg_dump run (scheduled + manual) with status
lifecycle, file metadata, and SHA-256 checksum for integrity verification.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd4e5f6a7b8c9'
down_revision = 'c1d2e3f4a5b6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'backup_records',
        sa.Column('backup_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('label', sa.String(length=80), nullable=False, unique=True),
        sa.Column('type', sa.String(length=20), nullable=False, server_default='manual'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('size_bytes', sa.BigInteger(), nullable=True),
        sa.Column('filename', sa.String(length=120), nullable=True),
        sa.Column('storage_path', sa.Text(), nullable=True),
        sa.Column('checksum_sha256', sa.String(length=64), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('triggered_by_email', sa.String(length=254), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_backup_records_created_at', 'backup_records', ['created_at'])
    op.create_index('ix_backup_records_status', 'backup_records', ['status'])


def downgrade() -> None:
    op.drop_index('ix_backup_records_status', table_name='backup_records')
    op.drop_index('ix_backup_records_created_at', table_name='backup_records')
    op.drop_table('backup_records')
