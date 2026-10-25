from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.promo_code import PromoCode, PromoCodeRedemption, PromoCodeScope
from app.repositories.base import BaseRepository


class PromoCodeRepository(BaseRepository[PromoCode]):
    """Promo code CRUD, including the wholesale scope replace the create/edit
    form's save-the-whole-thing UX needs (same pattern as
    NominationFormRepository.replace_fields)."""

    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_by_code(self, code: str) -> Optional[PromoCode]:
        result = await self.session.execute(
            select(PromoCode).where(PromoCode.code == code.strip().upper())
        )
        return result.scalar_one_or_none()

    async def get_by_org(self, org_id: UUID) -> List[PromoCode]:
        result = await self.session.execute(
            select(PromoCode)
            .where(PromoCode.org_id == org_id)
            .order_by(PromoCode.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_by_creator(self, user_id: UUID) -> List[PromoCode]:
        """For a user in no organization — their promo codes are personal,
        same "org_id IS NULL" rule as Election/Event."""
        result = await self.session.execute(
            select(PromoCode)
            .where(PromoCode.org_id.is_(None), PromoCode.created_by == user_id)
            .order_by(PromoCode.created_at.desc())
        )
        return list(result.scalars().all())

    async def replace_scope(self, promo_code_id: UUID, scope: List[dict]) -> None:
        existing = await self.session.execute(
            select(PromoCodeScope).where(PromoCodeScope.promo_code_id == promo_code_id)
        )
        for s in existing.scalars().all():
            await self.session.delete(s)
        await self.session.flush()

        for item in scope:
            self.session.add(PromoCodeScope(promo_code_id=promo_code_id, **item))
        await self.session.flush()

        # See NominationFormRepository.replace_fields for why this is needed:
        # without it, a caller that already loaded .scopes before this call
        # would see the stale, pre-replace list on any later selectin reload.
        promo = await self.session.get(PromoCode, promo_code_id)
        if promo:
            self.session.expire(promo, ["scopes"])

    async def count_redemptions(self, promo_code_id: UUID) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(PromoCodeRedemption).where(
                PromoCodeRedemption.promo_code_id == promo_code_id
            )
        )
        return result.scalar_one()

    async def count_redemptions_by_email(self, promo_code_id: UUID, email: str) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(PromoCodeRedemption).where(
                PromoCodeRedemption.promo_code_id == promo_code_id,
                func.lower(PromoCodeRedemption.email) == email.lower(),
            )
        )
        return result.scalar_one()

    async def counts_for_codes(self, promo_code_ids: List[UUID]) -> dict:
        """Bulk uses_count lookup for a list listing, avoiding one COUNT
        query per row."""
        if not promo_code_ids:
            return {}
        result = await self.session.execute(
            select(PromoCodeRedemption.promo_code_id, func.count())
            .where(PromoCodeRedemption.promo_code_id.in_(promo_code_ids))
            .group_by(PromoCodeRedemption.promo_code_id)
        )
        return dict(result.all())

    async def record_redemption(
        self,
        promo_code_id: UUID,
        *,
        channel: str,
        email: str,
        discount_amount: Decimal,
        user_id: Optional[UUID] = None,
    ) -> PromoCodeRedemption:
        redemption = PromoCodeRedemption(
            promo_code_id=promo_code_id,
            channel=channel,
            user_id=user_id,
            email=email,
            discount_amount=discount_amount,
        )
        self.session.add(redemption)
        await self.session.flush()
        return redemption
