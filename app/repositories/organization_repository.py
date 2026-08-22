from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.organization import Organization, OrganizationInvitation, OrganizationMember
from app.repositories.base import BaseRepository


class OrganizationRepository(BaseRepository[Organization]):
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
        self, member_id: UUID, role: str
    ) -> Optional[OrganizationMember]:
        result = await self.session.execute(
            select(OrganizationMember).where(
                OrganizationMember.member_id == member_id
            )
        )
        member = result.scalar_one_or_none()
        if member:
            member.role = role
            await self.session.flush()
            await self.session.refresh(member)
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
