from typing import Optional

from app.models.organization import OrganizationMember

# Fixed, curated set of org-scoped permission flags a custom OrgRole can be
# built from. Deliberately small — each one is wired to a real check
# somewhere, not decorative. Extend this list only alongside the check(s)
# that actually enforce the new flag.
MANAGE_TEAM = "manage_team"
MANAGE_PAYOUTS = "manage_payouts"
VIEW_FINANCIALS = "view_financials"
MANAGE_SETTINGS = "manage_settings"

ORG_PERMISSIONS = [MANAGE_TEAM, MANAGE_PAYOUTS, VIEW_FINANCIALS, MANAGE_SETTINGS]


def member_has_permission(member: Optional[OrganizationMember], permission: str) -> bool:
    """Owner/admin always pass — the coarse role is a superset of every
    custom permission. Anyone else needs an assigned OrgRole that lists the
    flag explicitly. `member.org_role` must already be loaded (selectin)."""
    if not member:
        return False
    if member.role in ("owner", "admin"):
        return True
    return bool(member.org_role and permission in member.org_role.permissions)
