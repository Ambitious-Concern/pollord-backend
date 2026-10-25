"""create org_roles and add org_role_id to organization_members

Revision ID: e8b2d5f4a1c9
Revises: d4a7f1c9e3b6
Create Date: 2026-09-19 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e8b2d5f4a1c9'
down_revision: Union[str, None] = 'd4a7f1c9e3b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'org_roles',
        sa.Column('org_role_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'org_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('organizations.org_id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('permissions', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_org_roles_org_id', 'org_roles', ['org_id'])

    op.add_column(
        'organization_members',
        sa.Column(
            'org_role_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('org_roles.org_role_id', ondelete='SET NULL'),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column('organization_members', 'org_role_id')
    op.drop_index('ix_org_roles_org_id', table_name='org_roles')
    op.drop_table('org_roles')
