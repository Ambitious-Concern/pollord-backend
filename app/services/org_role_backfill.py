"""Grant the organizer platform roles to organization members missing them.

Accepting an invitation used to grant Election Administrator / Event Organizer
only when the invite role was exactly "admin". Anyone added as editor or
member kept whatever they had — usually just Voter — and the organizer app
gates its dashboard on those roles, so they could sign in and see nothing.

The code now grants them to every member, but that does nothing for the
people already added. This closes that gap. It is idempotent: it only touches
members who are missing a role, so it is safe to re-run.

Write access is unaffected. It follows from the membership role via
OrganizationRepository.can_create, not from these platform roles.
"""
from typing import List
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import OrganizationMember
from app.models.user import Role, UserRole

MEMBER_PLATFORM_ROLES = ("Election Administrator", "Event Organizer")


async def backfill_member_platform_roles(session: AsyncSession) -> List[UUID]:
    """Grant any missing member platform roles. Returns the users changed."""
    roles = (
        await session.execute(
            select(Role).where(Role.role_name.in_(MEMBER_PLATFORM_ROLES))
        )
    ).scalars().all()
    role_ids = {r.role_name: r.role_id for r in roles}

    member_ids = set(
        (await session.execute(select(OrganizationMember.user_id))).scalars().all()
    )
    if not member_ids or not role_ids:
        return []

    existing = set(
        (
            await session.execute(
                select(UserRole.user_id, UserRole.role_id).where(
                    UserRole.user_id.in_(member_ids),
                    UserRole.role_id.in_(role_ids.values()),
                )
            )
        ).all()
    )

    changed: List[UUID] = []
    for user_id in member_ids:
        missing = [
            role_id
            for role_id in role_ids.values()
            if (user_id, role_id) not in existing
        ]
        if not missing:
            continue
        for role_id in missing:
            session.add(UserRole(user_id=user_id, role_id=role_id))
        changed.append(user_id)

    if changed:
        await session.flush()
    return changed
