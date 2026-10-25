"""A single, fixed organization ("Pollord Platform Org") owned by "Pollord
Admin" — the exact login credentials requested for demoing/testing every
organizer-facing v2 dashboard feature end to end, with a small real team
and a couple of invitations still sitting pending."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.organization import Organization, OrganizationInvitation, OrganizationMember
from app.models.user import User
from app.repositories.user_repository import UserRepository
from seeders import config
from seeders.data import images, names

# (org role, display title) — title is just for realistic naming, the
# actual permission surface is the "role" column (owner/editor/member).
_TEAM_ROLES = [
    ("owner", "Elections & Events Lead"),
    ("editor", "Finance Lead"),
    ("editor", "Events Coordinator"),
    ("member", "Support Lead"),
    ("member", "Field Agent"),
]
_INVITE_ROLES = ["editor", "member"]


async def _get_or_create_user(db: AsyncSession, email: str, full_name: str) -> User:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user:
        return user
    user = User(
        email=email,
        password_hash=hash_password(config.SEED_PASSWORD),
        full_name=full_name,
        email_verified=True,
        account_status="active",
    )
    db.add(user)
    await db.flush()
    return user


async def seed_organizations(db: AsyncSession) -> tuple[Organization, User, list[User]]:
    """Returns (org, admin, members) — admin is always members[0]."""
    user_repo = UserRepository(User, db)

    admin = await _get_or_create_user(db, config.ADMIN_EMAIL, config.ADMIN_NAME)
    # Fixed password always wins, even if this user was created under the
    # old generic SEED_PASSWORD by a previous run.
    admin.password_hash = hash_password(config.ADMIN_PASSWORD)
    await user_repo.grant_roles_by_name(admin.user_id, ["Election Administrator", "Event Organizer"])

    result = await db.execute(select(Organization).where(Organization.name == config.ORG_NAME))
    org = result.scalar_one_or_none()
    if not org:
        org = Organization(
            name=config.ORG_NAME,
            description=(
                "Pollord's own internal organization — the fixed test account used to "
                "demo and QA every organizer-facing feature end to end."
            ),
            logo_url=images.org_logo(0),
            owner_id=admin.user_id,
            is_verified=True,
            industry="Events & Media",
            email=config.ADMIN_EMAIL,
            website="https://pollord.com",
        )
        db.add(org)
        await db.flush()
        db.add(OrganizationMember(org_id=org.org_id, user_id=admin.user_id, role="owner"))
        await db.flush()

    members = [admin]
    seen_names: set = set()
    for i, (role, _title) in enumerate(_TEAM_ROLES[1 : config.N_TEAM_MEMBERS], start=1):
        email = f"seed+team{i}@pollord.dev"
        member = await _get_or_create_user(db, email, names.unique_full_name(seen_names))
        await user_repo.grant_roles_by_name(member.user_id, ["Election Administrator", "Event Organizer"])

        result = await db.execute(
            select(OrganizationMember).where(
                OrganizationMember.org_id == org.org_id, OrganizationMember.user_id == member.user_id
            )
        )
        if not result.scalar_one_or_none():
            db.add(OrganizationMember(
                org_id=org.org_id, user_id=member.user_id, role=role, invited_by=admin.user_id,
            ))
        members.append(member)
    await db.flush()

    for i in range(config.N_PENDING_INVITES):
        email = f"seed+invite{i}@pollord.dev"
        result = await db.execute(
            select(OrganizationInvitation).where(
                OrganizationInvitation.org_id == org.org_id, OrganizationInvitation.email == email,
            )
        )
        if result.scalar_one_or_none():
            continue
        db.add(OrganizationInvitation(
            org_id=org.org_id,
            email=email,
            role=_INVITE_ROLES[i % len(_INVITE_ROLES)],
            status="pending",
            invited_by=admin.user_id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        ))
    await db.flush()

    return org, admin, members
