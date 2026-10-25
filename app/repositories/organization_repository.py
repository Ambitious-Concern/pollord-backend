from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.organization import Organization, OrganizationInvitation, OrganizationMember
from app.repositories.base import BaseRepository


class OrganizationRepository(BaseRepository[Organization]):
    """Organization/membership queries: teammate resolution, ownership
    checks, invitations."""

    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_with_members(self, org_id: UUID) -> Optional[Organization]:
        result = await self.session.execute(
            select(Organization)
            .options(
                selectinload(Organization.members).selectinload(OrganizationMember.user),
                selectinload(Organization.owner),
            )
            .where(Organization.org_id == org_id)
        )
        return result.scalar_one_or_none()

    async def get_by_owner(self, user_id: UUID) -> List[Organization]:
        result = await self.session.execute(
            select(Organization)
            .options(
                selectinload(Organization.members).selectinload(OrganizationMember.user)
            )
            .where(Organization.owner_id == user_id)
            .order_by(Organization.created_at.desc())
        )
        return list(result.scalars().all())

    # Roles that imply write access to the organization's events/elections.
    # A plain "member" can see the org's work but not change it.
    MANAGING_ROLES = ("owner", "admin", "editor")

    async def get_teammate_ids(self, user_id: UUID) -> List[UUID]:
        """Every user who shares an organization with this one, plus themselves.

        Events and elections carry no org_id — they're owned by the user who
        created them — so "the organization's data" can only be expressed as
        "anything created by someone on my team". Without this, a newly added
        member sees an empty dashboard and appears to be in an org of one.

        Always includes `user_id`, so a user with no organization still sees
        their own work rather than nothing.
        """
        result = await self.session.execute(
            select(OrganizationMember.user_id).where(
                OrganizationMember.org_id.in_(
                    select(OrganizationMember.org_id).where(
                        OrganizationMember.user_id == user_id
                    )
                )
            )
        )
        return list({row[0] for row in result.all()} | {user_id})

    async def get_member_ids(self, org_id: UUID) -> List[UUID]:
        """Every user_id belonging to one organization — who should be
        notified about something that happened in it."""
        result = await self.session.execute(
            select(OrganizationMember.user_id).where(OrganizationMember.org_id == org_id)
        )
        return [row[0] for row in result.all()]

    async def get_org_ids_for_user(self, user_id: UUID) -> List[UUID]:
        """Every organization this user belongs to. Unlike
        get_primary_org_id_for_user (which picks ONE org for attributing a
        newly-created election/event), row-level security needs the FULL
        set — a multi-org user must keep seeing every org's data through
        RLS exactly as they already do today via get_teammate_ids, not just
        whichever org happens to be "primary"."""
        result = await self.session.execute(
            select(OrganizationMember.org_id).where(
                OrganizationMember.user_id == user_id
            )
        )
        return [row[0] for row in result.all()]

    async def get_primary_org_id_for_user(self, user_id: UUID) -> Optional[UUID]:
        """Which single org a new election/event this user creates should be
        attributed to. A user can belong to more than one organization, so
        this picks one deterministically: their 'owner' membership first,
        else whichever they joined earliest. None if they're in no
        organization at all — that election/event is personal (see
        Election.org_id's docstring), same rule `can_create` already applies
        to whether they may create in the first place.

        Kept in sync with the tie-break used by the org_id backfill migration
        (alembic) so historical and newly-created rows resolve identically.
        """
        result = await self.session.execute(
            select(OrganizationMember.org_id)
            .where(OrganizationMember.user_id == user_id)
            .order_by(
                (OrganizationMember.role == "owner").desc(),
                OrganizationMember.joined_at.asc(),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def can_create(self, user_id: UUID) -> bool:
        """Whether the user may create new elections/events.

        can_manage_with answers "may I change this existing thing?" by
        comparing against its creator. Creation has no creator to compare
        against, so it needs its own rule: someone in no organization is
        their own organizer and may create freely, while someone in an
        organization needs a managing role there. Without this, granting a
        plain member the platform roles they need to *see* the org's work
        would also let them add to it.
        """
        result = await self.session.execute(
            select(OrganizationMember.role).where(
                OrganizationMember.user_id == user_id
            )
        )
        roles = [row[0] for row in result.all()]
        if not roles:
            return True
        return any(r in self.MANAGING_ROLES for r in roles)

    async def can_manage_with(self, user_id: UUID, creator_id: UUID) -> bool:
        """Whether `user_id` may manage something created by `creator_id`.

        True when they're the same person, or when they share an organization
        in which `user_id` holds a managing role.
        """
        if user_id == creator_id:
            return True

        result = await self.session.execute(
            select(OrganizationMember.member_id)
            .where(
                OrganizationMember.user_id == user_id,
                OrganizationMember.role.in_(self.MANAGING_ROLES),
                OrganizationMember.org_id.in_(
                    select(OrganizationMember.org_id).where(
                        OrganizationMember.user_id == creator_id
                    )
                ),
            )
            .limit(1)
        )
        return result.first() is not None

    async def get_user_organizations(self, user_id: UUID) -> List[Organization]:
        """Get all organizations a user belongs to (as owner or member)."""
        result = await self.session.execute(
            select(Organization)
            .options(
                selectinload(Organization.members).selectinload(OrganizationMember.user)
            )
            .where(
                Organization.org_id.in_(
                    select(OrganizationMember.org_id).where(
                        OrganizationMember.user_id == user_id
                    )
                )
            )
            .order_by(Organization.created_at.desc())
        )
        return list(result.scalars().all())

    async def add_member(
        self,
        org_id: UUID,
        user_id: UUID,
        role: str = "member",
        invited_by: Optional[UUID] = None,
    ) -> OrganizationMember:
        member = OrganizationMember(
            org_id=org_id,
            user_id=user_id,
            role=role,
            invited_by=invited_by,
        )
        self.session.add(member)
        await self.session.flush()
        await self.session.refresh(member)
        return member

    async def get_member(self, org_id: UUID, user_id: UUID) -> Optional[OrganizationMember]:
        result = await self.session.execute(
            select(OrganizationMember).where(
                OrganizationMember.org_id == org_id,
                OrganizationMember.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def update_member_role(
        self, member_id: UUID, role: str, org_role_id: Optional[UUID] = None
    ) -> Optional[OrganizationMember]:
        result = await self.session.execute(
            select(OrganizationMember).where(
                OrganizationMember.member_id == member_id
            )
        )
        member = result.scalar_one_or_none()
        if member:
            member.role = role
            member.org_role_id = org_role_id
            await self.session.flush()
            await self.session.refresh(member, attribute_names=["org_role"])
        return member

    async def remove_member(self, member_id: UUID) -> bool:
        result = await self.session.execute(
            select(OrganizationMember).where(
                OrganizationMember.member_id == member_id
            )
        )
        member = result.scalar_one_or_none()
        if member:
            await self.session.delete(member)
            await self.session.flush()
            return True
        return False

    # ── Invitations ────────────────────────────────────────────────────────────

    async def create_invitation(
        self,
        org_id: UUID,
        email: str,
        role: str,
        invited_by: Optional[UUID],
        expires_at: datetime,
    ) -> OrganizationInvitation:
        inv = OrganizationInvitation(
            org_id=org_id,
            email=email.lower(),
            role=role,
            invited_by=invited_by,
            expires_at=expires_at,
        )
        self.session.add(inv)
        await self.session.flush()
        await self.session.refresh(inv)
        return inv

    async def get_pending_invitation(
        self, org_id: UUID, email: str
    ) -> Optional[OrganizationInvitation]:
        result = await self.session.execute(
            select(OrganizationInvitation).where(
                OrganizationInvitation.org_id == org_id,
                OrganizationInvitation.email == email.lower(),
                OrganizationInvitation.status == "pending",
            )
        )
        return result.scalar_one_or_none()

    async def get_invitation_by_token(self, token: str) -> Optional[OrganizationInvitation]:
        result = await self.session.execute(
            select(OrganizationInvitation).where(
                OrganizationInvitation.token == token
            )
        )
        return result.scalar_one_or_none()

    async def accept_invitation(self, invitation_id: UUID) -> None:
        result = await self.session.execute(
            select(OrganizationInvitation).where(
                OrganizationInvitation.invitation_id == invitation_id
            )
        )
        inv = result.scalar_one_or_none()
        if inv:
            inv.status = "accepted"
            await self.session.flush()

    async def get_pending_invitations_for_org(self, org_id: UUID) -> List[OrganizationInvitation]:
        result = await self.session.execute(
            select(OrganizationInvitation)
            .where(OrganizationInvitation.org_id == org_id, OrganizationInvitation.status == "pending")
            .order_by(OrganizationInvitation.created_at.desc())
        )
        return list(result.scalars().all())

    # ── Ownership transfer / deactivation ─────────────────────────────────────

    async def transfer_ownership(
        self,
        org: Organization,
        current_owner_member: OrganizationMember,
        new_owner_member: OrganizationMember,
    ) -> Organization:
        org.owner_id = new_owner_member.user_id
        new_owner_member.role = "owner"
        current_owner_member.role = "admin"
        await self.session.flush()
        await self.session.refresh(org)
        return org

    async def deactivate(self, org_id: UUID) -> None:
        org = await self.get_by_id(org_id, id_field="org_id")
        if org:
            org.is_active = False
            org.deactivated_at = datetime.now(timezone.utc)
            await self.session.flush()

    async def delete_invitation(self, org_id: UUID, invitation_id: UUID) -> bool:
        result = await self.session.execute(
            select(OrganizationInvitation).where(
                OrganizationInvitation.invitation_id == invitation_id,
                OrganizationInvitation.org_id == org_id,
            )
        )
        inv = result.scalar_one_or_none()
        if not inv:
            return False
        await self.session.delete(inv)
        await self.session.flush()
        return True
