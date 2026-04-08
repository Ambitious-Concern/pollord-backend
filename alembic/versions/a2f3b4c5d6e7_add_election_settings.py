"""add election settings and visibility

Revision ID: a2f3b4c5d6e7
Revises: 1dce911a294e
Create Date: 2026-03-17 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "a2f3b4c5d6e7"
down_revision: Union[str, None] = "1dce911a294e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Election settings columns
    op.add_column("elections", sa.Column("banner_image_url", sa.String(1000), nullable=True))
    op.add_column("elections", sa.Column("visibility", sa.String(20), server_default="public", nullable=False))
    op.add_column("elections", sa.Column("access_code", sa.String(50), nullable=True))
    op.add_column("elections", sa.Column("allow_result_viewing", sa.String(20), server_default="after_end", nullable=False))
    op.add_column("elections", sa.Column("require_verification", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("elections", sa.Column("anonymous_results", sa.Boolean(), server_default="true", nullable=False))
    op.add_column("elections", sa.Column("allow_abstain", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("elections", sa.Column("show_candidate_count", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("elections", sa.Column("randomize_candidate_order", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("elections", sa.Column("enable_notifications", sa.Boolean(), server_default="true", nullable=False))
    op.add_column("elections", sa.Column("max_selections", sa.Integer(), nullable=True))
    op.add_column("elections", sa.Column("settings_extra", postgresql.JSONB(), nullable=True))

    # Increase candidate image_url length
    op.alter_column("candidates", "image_url", type_=sa.String(1000), existing_type=sa.String(500))

    # Index on visibility for public queries
    op.create_index("ix_elections_visibility", "elections", ["visibility"])


def downgrade() -> None:
    op.drop_index("ix_elections_visibility", table_name="elections")
    op.alter_column("candidates", "image_url", type_=sa.String(500), existing_type=sa.String(1000))
    op.drop_column("elections", "settings_extra")
    op.drop_column("elections", "max_selections")
    op.drop_column("elections", "enable_notifications")
    op.drop_column("elections", "randomize_candidate_order")
    op.drop_column("elections", "show_candidate_count")
    op.drop_column("elections", "allow_abstain")
    op.drop_column("elections", "anonymous_results")
    op.drop_column("elections", "require_verification")
    op.drop_column("elections", "allow_result_viewing")
    op.drop_column("elections", "access_code")
    op.drop_column("elections", "visibility")
    op.drop_column("elections", "banner_image_url")
