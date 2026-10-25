"""create rls app role and policies

Revision ID: a834750eb41e
Revises: 7aae0f9d9ed0
Create Date: 2026-09-15 14:02:14.639936

Schema-only, zero behavior change for the live app: this creates a brand new
Postgres role (pollord_app_rls) that nothing currently authenticates as, and
grants/policies attached to it. The app keeps connecting as its existing
superuser/table-owner role, which bypasses row-level security unconditionally
regardless of anything below. Switching the app to pollord_app_rls is a
separate, deliberately deferred cutover (see the RLS plan) — this migration
is safe to ship to production immediately on its own.

Personal (org-less) elections/events — org_id IS NULL, see Election.org_id's
docstring — are deliberately left visible under every policy below rather
than hidden. RLS's job here is isolating one org's data from another org;
personal resources have no org to isolate by, and stay governed by the
existing created_by-based checks at the application layer, exactly as today.
That's why every predicate reads "parent org is NULL, or parent org is one of
mine" rather than just the latter — dropping the NULL branch would silently
make every personal election/event (and everything under it) invisible the
moment the app ever connects as this role.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.core.config import settings

# revision identifiers, used by Alembic.
revision: str = 'a834750eb41e'
down_revision: Union[str, None] = '7aae0f9d9ed0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RLS_ROLE = "pollord_app_rls"

# Every table this phase enforces org isolation on, grouped by how it reaches
# its owning org — mirrors the scoping map in the RLS plan exactly.
ROOT_TABLES = ["elections", "events"]                              # own org_id
ELECTION_CHILD_TABLES = ["eligible_voters", "candidate_access_otps", "vote_receipts"]
EVENT_CHILD_TABLES = ["ticket_types", "tickets", "ticket_purchases"]
CATEGORY_CHILD_TABLES = ["candidates", "voter_otps"]                # via category_id
DUAL_PARENT_TABLES = ["categories", "votes", "payout_requests"]     # election_id OR event_id

ALL_TENANT_TABLES = (
    ROOT_TABLES + ELECTION_CHILD_TABLES + EVENT_CHILD_TABLES
    + CATEGORY_CHILD_TABLES + DUAL_PARENT_TABLES
)

# transactions/ticket_transactions are deliberately excluded — their
# election_id/event_id/user_id columns have no FK at all (existing
# convention, see app/models/transaction.py), so a policy trusting them would
# be false confidence rather than real isolation. audit_logs is excluded too
# (entity_id is polymorphic, no single join predicate applies) and stays
# admin-only via the existing role checks, same as today.


def upgrade() -> None:
    if not settings.RLS_APP_ROLE_PASSWORD:
        raise RuntimeError(
            "RLS_APP_ROLE_PASSWORD is not set — refusing to create "
            f"'{RLS_ROLE}' with no password. Set it in the environment "
            "running this migration and retry."
        )

    # --- 1. the restricted role itself -------------------------------------
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{RLS_ROLE}') THEN
                CREATE ROLE {RLS_ROLE} WITH LOGIN NOSUPERUSER NOCREATEDB
                    NOCREATEROLE NOBYPASSRLS NOINHERIT;
            END IF;
        END
        $$;
        """
    )
    # ALTER ROLE ... WITH PASSWORD does not accept bind parameters at all —
    # Postgres rejects a $1 placeholder there with a syntax error, since it's
    # parsed before the extended-query protocol's parameter substitution
    # applies. Standard single-quote doubling is the correct, safe way to
    # embed a literal value here instead.
    escaped_pwd = settings.RLS_APP_ROLE_PASSWORD.replace("'", "''")
    op.execute(f"ALTER ROLE {RLS_ROLE} WITH PASSWORD '{escaped_pwd}'")
    op.execute(f"GRANT CONNECT ON DATABASE {_current_database()} TO {RLS_ROLE}")
    # Postgres doesn't grant USAGE on the public schema to new roles by
    # default — without this, every table grant below is inert and any
    # unqualified (or even schema-qualified) query from this role fails with
    # "relation does not exist" / "permission denied for schema public".
    # Found empirically: table grants alone were not enough to make any
    # table visible at all.
    op.execute(f"GRANT USAGE ON SCHEMA public TO {RLS_ROLE}")

    # --- 2. grants — exactly the phase-1 table list, nothing else ----------
    for table in ALL_TENANT_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {RLS_ROLE}")

    # --- 3. SQL helper functions --------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION current_org_ids() RETURNS uuid[]
        LANGUAGE sql STABLE AS $$
            SELECT string_to_array(
                NULLIF(current_setting('app.org_ids', true), ''), ','
            )::uuid[]
        $$;
        """
    )
    # SECURITY DEFINER (+ a pinned search_path, standard hardening against
    # search_path hijacking) is load-bearing here, not decoration: these
    # functions look up a PARENT row (an election/event/category) to decide
    # whether a CHILD row is visible. Elections/events/categories all carry
    # their own RLS policy too, so a plain (non-DEFINER) function run as
    # pollord_app_rls would have another org's parent row hidden from IT as
    # well — indistinguishable from "no parent at all", which every policy
    # below treats as an unscoped/personal resource and lets through. Found
    # empirically: without this, another org's category/candidate rows (and
    # inserts into another org's election) were incorrectly visible/allowed.
    # SECURITY DEFINER makes these specific, narrow, read-only lookups run
    # with the owning (migration-running) role's privileges so they see the
    # real org_id regardless of the caller's own RLS view — the actual
    # row-by-row filtering the caller experiences still happens in the
    # policy itself, not here.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION election_org_id(p_election_id uuid) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
            SELECT org_id FROM elections WHERE election_id = p_election_id
        $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION event_org_id(p_event_id uuid) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
            SELECT org_id FROM events WHERE event_id = p_event_id
        $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION parent_org_id(p_election_id uuid, p_event_id uuid)
        RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
            SELECT COALESCE(election_org_id(p_election_id), event_org_id(p_event_id))
        $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION category_org_id(p_category_id uuid) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
            SELECT parent_org_id(c.election_id, c.event_id)
            FROM categories c WHERE c.category_id = p_category_id
        $$;
        """
    )

    # --- 4. enable + force RLS, one policy per table ------------------------
    for table in ROOT_TABLES:
        _apply_policy(table, "org_id IS NULL OR org_id = ANY(current_org_ids())")

    for table in ELECTION_CHILD_TABLES:
        _apply_policy(
            table,
            "election_org_id(election_id) IS NULL "
            "OR election_org_id(election_id) = ANY(current_org_ids())",
        )

    for table in EVENT_CHILD_TABLES:
        _apply_policy(
            table,
            "event_org_id(event_id) IS NULL "
            "OR event_org_id(event_id) = ANY(current_org_ids())",
        )

    for table in CATEGORY_CHILD_TABLES:
        _apply_policy(
            table,
            "category_org_id(category_id) IS NULL "
            "OR category_org_id(category_id) = ANY(current_org_ids())",
        )

    for table in DUAL_PARENT_TABLES:
        _apply_policy(
            table,
            "parent_org_id(election_id, event_id) IS NULL "
            "OR parent_org_id(election_id, event_id) = ANY(current_org_ids())",
        )


def downgrade() -> None:
    for table in ALL_TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON {table} FROM {RLS_ROLE}")

    op.execute("DROP FUNCTION IF EXISTS category_org_id(uuid)")
    op.execute("DROP FUNCTION IF EXISTS parent_org_id(uuid, uuid)")
    op.execute("DROP FUNCTION IF EXISTS event_org_id(uuid)")
    op.execute("DROP FUNCTION IF EXISTS election_org_id(uuid)")
    op.execute("DROP FUNCTION IF EXISTS current_org_ids()")

    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {RLS_ROLE}")
    op.execute(f"REVOKE CONNECT ON DATABASE {_current_database()} FROM {RLS_ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {RLS_ROLE}")


def _apply_policy(table: str, predicate: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    # FORCE costs nothing today (the app's role stays owner+superuser and
    # bypasses regardless) — it's insurance against a future accidental
    # `ALTER TABLE ... OWNER TO <app_role>` silently disabling enforcement.
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{table} ON {table}
        USING ({predicate})
        WITH CHECK ({predicate})
        """
    )


def _current_database() -> str:
    return op.get_bind().engine.url.database
