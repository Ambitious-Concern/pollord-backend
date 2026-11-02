"""create app logs

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-10-28

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'app_logs',
        sa.Column('log_id', postgresql.UUID(as_uuid=True), nullable=False, default=sa.text('gen_random_uuid()')),
        sa.Column('level', sa.String(20), nullable=False),
        sa.Column('source', sa.String(300), nullable=False),
        sa.Column('logger_name', sa.String(200), nullable=False),
        sa.Column('message', sa.Text, nullable=False),
        sa.Column('traceback', sa.Text, nullable=True),
        sa.Column('fingerprint', sa.String(16), nullable=False),
        sa.Column('request_id', sa.String(64), nullable=True),
        sa.Column('request_method', sa.String(10), nullable=True),
        sa.Column('request_path', sa.String(500), nullable=True),
        sa.Column('request_user_email', sa.String(200), nullable=True),
        sa.Column('request_ip', sa.String(45), nullable=True),
        sa.Column('http_status', sa.Integer, nullable=True),
        sa.Column('extra', postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column('resolved', sa.Boolean, nullable=False, server_default='false'),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_by_email', sa.String(200), nullable=True),
        sa.Column('occurrence_count', sa.Integer, nullable=False, server_default='1'),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('log_id'),
    )
    op.create_index('ix_app_logs_fingerprint', 'app_logs', ['fingerprint'])
    op.create_index('ix_app_logs_level_created', 'app_logs', ['level', 'created_at'])
    op.create_index('ix_app_logs_resolved_created', 'app_logs', ['resolved', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_app_logs_resolved_created', 'app_logs')
    op.drop_index('ix_app_logs_level_created', 'app_logs')
    op.drop_index('ix_app_logs_fingerprint', 'app_logs')
    op.drop_table('app_logs')
