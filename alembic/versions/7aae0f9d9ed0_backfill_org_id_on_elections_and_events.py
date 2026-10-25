"""backfill org_id on elections and events

Revision ID: 7aae0f9d9ed0
Revises: 76eb6adeab0b
Create Date: 2026-09-15 13:58:26.124390

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7aae0f9d9ed0'
down_revision: Union[str, None] = '76eb6adeab0b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Data-only migration: resolves each existing election/event's org from
    # its creator's organization memberships. Tie-break for a creator in more
    # than one org: their 'owner' membership first, else whichever they
    # joined earliest — kept in sync with
    # OrganizationRepository.get_primary_org_id_for_user, which applies the
    # same rule to newly-created rows going forward.
    #
    # A creator in no organization at all is left NULL on purpose — that
    # election/event is personal, not a gap (see Election.org_id's
    # docstring). This migration never sets a NOT NULL constraint.
    primary_org_cte = """
        SELECT DISTINCT ON (om.user_id)
            om.user_id,
            om.org_id
        FROM organization_members om
        ORDER BY om.user_id, (om.role = 'owner') DESC, om.joined_at ASC
    """

    conn = op.get_bind()

    for table, fk_column in (("elections", "election_id"), ("events", "event_id")):
        result = conn.execute(
            sa.text(
                f"""
                WITH primary_org AS ({primary_org_cte})
                UPDATE {table} t
                SET org_id = po.org_id
                FROM primary_org po
                WHERE t.created_by = po.user_id
                  AND t.org_id IS NULL
                RETURNING t.{fk_column}
                """
            )
        )
        updated = result.rowcount
        remaining = conn.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE org_id IS NULL")
        ).scalar_one()
        print(
            f"[backfill_org_id] {table}: set org_id on {updated} row(s), "
            f"{remaining} row(s) left NULL (personal, creator belongs to no organization)"
        )


def downgrade() -> None:
    # Reversible in aggregate: re-running upgrade() recomputes the exact same
    # values deterministically from organization_members, so clearing back to
    # NULL (the column's state right after the previous migration) is a
    # faithful downgrade rather than a lossy approximation.
    op.execute("UPDATE elections SET org_id = NULL")
    op.execute("UPDATE events SET org_id = NULL")
