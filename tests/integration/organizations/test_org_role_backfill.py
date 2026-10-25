"""Backfill for members invited before acceptance granted the view roles.

Anyone added as editor or member before that fix holds no platform roles, so
they stay locked out of the organizer dashboard even after the code change.
This grants the roles to every existing member who is missing them.
"""
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.organization import Organization, OrganizationMember
from app.models.user import Role, User, UserRole
from app.services.org_role_backfill import backfill_member_platform_roles

VIEW_ROLES = ("Election Administrator", "Event Organizer")


async def _role(db: AsyncSession, name: str) -> Role:
    result = await db.execute(select(Role).where(Role.role_name == name))
    role = result.scalar_one_or_none()
    if not role:
        role = Role(role_name=name, permissions={})
        db.add(role)
        await db.flush()
    return role


async def _user(db: AsyncSession, email: str, roles=()) -> User:
    user = User(
        email=email,
        password_hash=hash_password("Passw0rd!"),
        full_name=email.split("@")[0].title(),
        email_verified=True,
        account_status="active",
    )
    db.add(user)
    await db.flush()
    for name in roles:
        r = await _role(db, name)
        db.add(UserRole(user_id=user.user_id, role_id=r.role_id))
    await db.flush()
    return user


async def _role_names(db: AsyncSession, user_id) -> set[str]:
    result = await db.execute(
        select(Role.role_name)
        .join(UserRole, UserRole.role_id == Role.role_id)
        .where(UserRole.user_id == user_id)
    )
    return {row[0] for row in result.all()}


@pytest.mark.asyncio
class TestBackfill:
    async def test_grants_view_roles_to_members_missing_them(
        self, db_session: AsyncSession
    ):
        # Both roles must exist for the backfill to be able to grant them.
        for name in VIEW_ROLES:
            await _role(db_session, name)

        owner = await _user(db_session, f"o-{uuid4().hex[:6]}@example.com", VIEW_ROLES)
        stranded = await _user(db_session, f"s-{uuid4().hex[:6]}@example.com")

        org = Organization(name="Legacy Org", owner_id=owner.user_id)
        db_session.add(org)
        await db_session.flush()
        db_session.add_all([
            OrganizationMember(org_id=org.org_id, user_id=owner.user_id, role="owner"),
            OrganizationMember(org_id=org.org_id, user_id=stranded.user_id, role="member"),
        ])
        await db_session.flush()

        assert not (set(VIEW_ROLES) & await _role_names(db_session, stranded.user_id))

        granted = await backfill_member_platform_roles(db_session)

        assert stranded.user_id in granted
        assert set(VIEW_ROLES) <= await _role_names(db_session, stranded.user_id)

    async def test_is_idempotent(self, db_session: AsyncSession):
        for name in VIEW_ROLES:
            await _role(db_session, name)

        owner = await _user(db_session, f"i-{uuid4().hex[:6]}@example.com")
        org = Organization(name="Idempotent Org", owner_id=owner.user_id)
        db_session.add(org)
        await db_session.flush()
        db_session.add(
            OrganizationMember(org_id=org.org_id, user_id=owner.user_id, role="owner")
        )
        await db_session.flush()

        first = await backfill_member_platform_roles(db_session)
        assert owner.user_id in first

        second = await backfill_member_platform_roles(db_session)
        assert owner.user_id not in second

    async def test_leaves_non_members_alone(self, db_session: AsyncSession):
        """Someone in no organization must not be handed organizer roles."""
        for name in VIEW_ROLES:
            await _role(db_session, name)

        loner = await _user(db_session, f"l-{uuid4().hex[:6]}@example.com")

        granted = await backfill_member_platform_roles(db_session)

        assert loner.user_id not in granted
        assert not (set(VIEW_ROLES) & await _role_names(db_session, loner.user_id))
