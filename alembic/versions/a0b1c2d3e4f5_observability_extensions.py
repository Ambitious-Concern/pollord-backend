"""observability extensions: release field + app_log_alerts table

Revision ID: a0b1c2d3e4f5
Revises: f3c9e1a7b5d2
Create Date: 2026-10-28 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a0b1c2d3e4f5'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('app_logs', sa.Column('release', sa.String(100), nullable=True))

    op.create_table(
        'app_log_alerts',
        sa.Column('alert_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('trigger_type', sa.String(30), nullable=False),
        sa.Column('level', sa.String(20), nullable=True),
        sa.Column('threshold_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('window_minutes', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('channel_type', sa.String(20), nullable=False),
        sa.Column('channel_destination', sa.String(500), nullable=False),
        sa.Column('cooldown_minutes', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('last_triggered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('app_log_alerts')
    op.drop_column('app_logs', 'release')
