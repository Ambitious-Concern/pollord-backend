"""create abuse_alerts table

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
Create Date: 2026-10-28 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'b1c2d3e4f5a6'
down_revision = 'a0b1c2d3e4f5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'abuse_alerts',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('alert_type', sa.String(30), nullable=False),
        sa.Column('severity', sa.String(10), nullable=False),
        sa.Column('source_ip', sa.String(45), nullable=True),
        sa.Column('source_user_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('target_path', sa.String(500), nullable=True),
        sa.Column('detail', postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column('status', sa.String(15), nullable=False, server_default='NEW'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(['source_user_id'], ['users.user_id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['resolved_by'], ['users.user_id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_abuse_alert_severity', 'abuse_alerts', ['severity'])
    op.create_index('idx_abuse_alert_status', 'abuse_alerts', ['status'])
    op.create_index('idx_abuse_alert_created', 'abuse_alerts', ['created_at'])
    op.create_index('idx_abuse_alert_source_ip', 'abuse_alerts', ['source_ip'])


def downgrade() -> None:
    op.drop_index('idx_abuse_alert_source_ip', table_name='abuse_alerts')
    op.drop_index('idx_abuse_alert_created', table_name='abuse_alerts')
    op.drop_index('idx_abuse_alert_status', table_name='abuse_alerts')
    op.drop_index('idx_abuse_alert_severity', table_name='abuse_alerts')
    op.drop_table('abuse_alerts')
