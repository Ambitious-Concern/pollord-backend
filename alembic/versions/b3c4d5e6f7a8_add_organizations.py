"""add organizations and members

Revision ID: b3c4d5e6f7a8
Revises: a2f3b4c5d6e7
Create Date: 2026-03-17 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, None] = "a2f3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("org_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("logo_url", sa.String(1000), nullable=True),
        sa.Column("website", sa.String(500), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("phone", sa.String(30), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("industry", sa.String(100), nullable=True),
        sa.Column("is_verified", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("owner_id", UUID(as_uuid=True), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_table(
        "organization_members",
        sa.Column("member_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("organizations.org_id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(30), server_default="member", nullable=False),
        sa.Column("invited_by", UUID(as_uuid=True), sa.ForeignKey("users.user_id"), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_index("ix_org_members_org_id", "organization_members", ["org_id"])
    op.create_index("ix_org_members_user_id", "organization_members", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_org_members_user_id", table_name="organization_members")
    op.drop_index("ix_org_members_org_id", table_name="organization_members")
    op.drop_table("organization_members")
    op.drop_table("organizations")
