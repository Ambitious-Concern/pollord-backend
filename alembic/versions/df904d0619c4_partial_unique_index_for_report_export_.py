"""partial unique index for report export idempotency

Revision ID: df904d0619c4
Revises: c4f8b2e6d9a1
Create Date: 2026-09-21 16:35:47.276182

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'df904d0619c4'
down_revision: Union[str, None] = 'c4f8b2e6d9a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("uq_report_export_idempotency", "report_export_jobs", type_="unique")
    op.create_index(
        "uq_report_export_idempotency",
        "report_export_jobs",
        ["requested_by", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("status != 'failed'"),
    )


def downgrade() -> None:
    op.drop_index("uq_report_export_idempotency", table_name="report_export_jobs")
    op.create_unique_constraint(
        "uq_report_export_idempotency",
        "report_export_jobs",
        ["requested_by", "idempotency_key"],
    )
