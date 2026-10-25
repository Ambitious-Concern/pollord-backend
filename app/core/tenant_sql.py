"""Pure formatting for the Postgres session GUC row-level security policies
read from. No DB access, no session mutation — just "list of UUIDs in, the
exact string the SQL-side current_org_ids() helper expects out" — kept
separate from resolving *which* org ids a user has (organization_repository)
and from actually setting the GUC on a session (app/db/tenant.py)."""
from typing import List
from uuid import UUID


def render_org_ids_literal(org_ids: List[UUID]) -> str:
    """Empty list -> empty string, never "None"/"[]" — current_org_ids() in
    Postgres treats an empty string as NULLIF'd to NULL, which every
    tenant-isolation policy in turn treats as "no org context, see nothing"."""
    return ",".join(str(org_id) for org_id in org_ids)
