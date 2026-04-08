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
