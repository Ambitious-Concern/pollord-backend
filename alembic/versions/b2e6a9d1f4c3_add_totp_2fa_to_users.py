"""add totp 2fa fields to users

Revision ID: b2e6a9d1f4c3
Revises: a1d4f6c8b3e7
Create Date: 2026-09-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2e6a9d1f4c3'
down_revision: Union[str, None] = 'a1d4f6c8b3e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('totp_secret_encrypted', sa.LargeBinary(), nullable=True))
    op.add_column(
        'users',
        sa.Column('totp_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column('users', sa.Column('recovery_codes_hashed', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'recovery_codes_hashed')
    op.drop_column('users', 'totp_enabled')
    op.drop_column('users', 'totp_secret_encrypted')
