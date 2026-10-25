from datetime import date
from decimal import ROUND_DOWN, Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ownership import (
    require_election_ownership,
    require_event_ownership,
    require_promo_code_ownership,
)
from app.models.election import Election
from app.models.event import Event
from app.models.org_activity_log import OrgActivityLog
from app.models.organization import Organization
from app.models.promo_code import PromoCode
from app.models.user import User
from app.repositories.election_repository import ElectionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.org_activity_log_repository import OrgActivityLogRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.promo_code_repository import PromoCodeRepository
from app.schemas.promo_code import (
    PromoCodeCreate,
    PromoCodeResponse,
    PromoCodeUpdate,
    PromoScopeInput,
    PromoScopeResponse,
)


class PromoCodeService:
    """Organizer CRUD for discount codes, plus the core pricing logic both
    the ticket and vote payment flows call to apply one."""

    def __init__(
        self,
        db: AsyncSession,
        promo_repo: PromoCodeRepository,
        org_repo: OrganizationRepository,
        election_repo: ElectionRepository,
        event_repo: EventRepository,
    ):
        self.db = db
        self.promo_repo = promo_repo
        self.org_repo = org_repo
        self.election_repo = election_repo
        self.event_repo = event_repo

    # --- Organizer CRUD ----------------------------------------------------

    async def _validate_scope_ownership(self, item: PromoScopeInput, user: User) -> None:
        if item.election_id:
            election = await self.election_repo.get_by_id(item.election_id, id_field="election_id")
            if not election:
                raise HTTPException(status_code=404, detail=f"Election {item.election_id} not found")
            await require_election_ownership(election, user, self.db)
        else:
            event = await self.event_repo.get_by_id(item.event_id, id_field="event_id")
            if not event:
                raise HTTPException(status_code=404, detail=f"Event {item.event_id} not found")
            await require_event_ownership(event, user, self.db)

    async def create(
        self,
        user: User,
        data: PromoCodeCreate,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> PromoCodeResponse:
        if not await self.org_repo.can_create(user.user_id):
            raise HTTPException(
                status_code=403,
                detail="Your organization role is view-only. Ask an owner or admin to give you edit access.",
            )
        if await self.promo_repo.get_by_code(data.code):
            raise HTTPException(status_code=409, detail=f"Code '{data.code}' is already in use")
        for item in data.scope:
            await self._validate_scope_ownership(item, user)

        org_id = await self.org_repo.get_primary_org_id_for_user(user.user_id)
        promo = await self.promo_repo.create(
            {
                "org_id": org_id,
                "created_by": user.user_id,
                "code": data.code,
                "discount_type": data.discount_type,
                "discount_value": data.discount_value,
                "usage_limit": data.usage_limit,
                "per_person_limit": data.per_person_limit,
                "valid_from": data.valid_from,
                "valid_until": data.valid_until,
                "is_active": data.is_active,
            }
        )
        await self.promo_repo.replace_scope(
            promo.promo_code_id,
            [item.model_dump(exclude_none=True) for item in data.scope],
        )

        if org_id is not None:
            await OrgActivityLogRepository(OrgActivityLog, self.db).log_activity(
                org_id=org_id,
                category="settings",
                verb="created a promo code",
                target_name=promo.code,
                user_id=user.user_id,
                ip_address=ip,
                user_agent=user_agent,
            )

        return await self._to_response(await self._get_or_404(promo.promo_code_id))

    async def update(
        self,
        promo_code_id: UUID,
        user: User,
        data: PromoCodeUpdate,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> PromoCodeResponse:
        promo = await self._get_or_404(promo_code_id)
        await require_promo_code_ownership(promo, user, self.db)

        existing = await self.promo_repo.get_by_code(data.code)
        if existing and existing.promo_code_id != promo_code_id:
            raise HTTPException(status_code=409, detail=f"Code '{data.code}' is already in use")
        for item in data.scope:
            await self._validate_scope_ownership(item, user)

        await self.promo_repo.update(
            promo_code_id,
            {
                "code": data.code,
                "discount_type": data.discount_type,
                "discount_value": data.discount_value,
                "usage_limit": data.usage_limit,
                "per_person_limit": data.per_person_limit,
                "valid_from": data.valid_from,
                "valid_until": data.valid_until,
                "is_active": data.is_active,
            },
            id_field="promo_code_id",
        )
        await self.promo_repo.replace_scope(
            promo_code_id,
            [item.model_dump(exclude_none=True) for item in data.scope],
        )

        if promo.org_id is not None:
            await OrgActivityLogRepository(OrgActivityLog, self.db).log_activity(
                org_id=promo.org_id,
                category="settings",
                verb="updated a promo code",
                target_name=data.code,
                user_id=user.user_id,
                ip_address=ip,
                user_agent=user_agent,
            )

        return await self._to_response(await self._get_or_404(promo_code_id))

    async def list_for_user(self, user: User) -> List[PromoCodeResponse]:
        org_id = await self.org_repo.get_primary_org_id_for_user(user.user_id)
        if org_id is None:
            promos = await self.promo_repo.get_by_creator(user.user_id)
        else:
            promos = await self.promo_repo.get_by_org(org_id)

        counts = await self.promo_repo.counts_for_codes([p.promo_code_id for p in promos])
        return [await self._to_response(p, uses_count=counts.get(p.promo_code_id, 0)) for p in promos]

    async def delete(self, promo_code_id: UUID, user: User) -> None:
        promo = await self._get_or_404(promo_code_id)
        await require_promo_code_ownership(promo, user, self.db)
        if await self.promo_repo.count_redemptions(promo_code_id) > 0:
            raise HTTPException(status_code=409, detail="This code has already been used and can't be deleted")
        await self.promo_repo.delete(promo_code_id, id_field="promo_code_id")

    async def _get_or_404(self, promo_code_id: UUID) -> PromoCode:
        promo = await self.promo_repo.get_by_id(promo_code_id, id_field="promo_code_id")
        if not promo:
            raise HTTPException(status_code=404, detail="Promo code not found")
        return promo

    async def _to_response(self, promo: PromoCode, uses_count: Optional[int] = None) -> PromoCodeResponse:
        if uses_count is None:
            uses_count = await self.promo_repo.count_redemptions(promo.promo_code_id)
        scope_out = []
        for s in promo.scopes:
            if s.election_id:
                election = await self.election_repo.get_by_id(s.election_id, id_field="election_id")
                scope_out.append(PromoScopeResponse(election_id=s.election_id, name=election.title if election else ""))
            else:
                event = await self.event_repo.get_by_id(s.event_id, id_field="event_id")
                scope_out.append(PromoScopeResponse(event_id=s.event_id, name=event.title if event else ""))
        return PromoCodeResponse(
            promo_code_id=promo.promo_code_id,
            code=promo.code,
            discount_type=promo.discount_type,
            discount_value=promo.discount_value,
            scope=scope_out,
            uses_count=uses_count,
            usage_limit=promo.usage_limit,
            per_person_limit=promo.per_person_limit,
            valid_from=promo.valid_from,
            valid_until=promo.valid_until,
            status=_compute_status(promo),
            created_at=promo.created_at,
        )

    # --- Checkout-time application ------------------------------------------

    async def resolve_for_checkout(
        self,
        code: str,
        *,
        election_id: Optional[UUID] = None,
        event_id: Optional[UUID] = None,
        email: str,
    ) -> PromoCode:
        """Validates a buyer-entered code against scope/limits/expiry.
        Raises HTTPException(400/404) on any failure — callers should
        surface that directly, it's always buyer-facing ("this code isn't
        valid for this purchase")."""
        promo = await self.promo_repo.get_by_code(code)
        if not promo:
            raise HTTPException(status_code=404, detail="Promo code not found")
        if not promo.is_active:
            raise HTTPException(status_code=400, detail="This promo code is not active")

        today = date.today()
        if today < promo.valid_from:
            raise HTTPException(status_code=400, detail="This promo code is not valid yet")
        if promo.valid_until and today > promo.valid_until:
            raise HTTPException(status_code=400, detail="This promo code has expired")

        if promo.scopes:
            in_scope = any(
                (s.election_id and s.election_id == election_id)
                or (s.event_id and s.event_id == event_id)
                for s in promo.scopes
            )
            if not in_scope:
                raise HTTPException(status_code=400, detail="This promo code doesn't apply to this purchase")

        # Counted against completed redemptions only, checked at initiation —
        # two concurrent checkouts can both pass this and later both redeem,
        # same tolerated race as any "reserve at checkout" discount scheme.
        if promo.usage_limit is not None:
            used = await self.promo_repo.count_redemptions(promo.promo_code_id)
            if used >= promo.usage_limit:
                raise HTTPException(status_code=400, detail="This promo code has reached its usage limit")

        used_by_person = await self.promo_repo.count_redemptions_by_email(promo.promo_code_id, email)
        if used_by_person >= promo.per_person_limit:
            raise HTTPException(status_code=400, detail="You've already used this promo code the maximum number of times")

        return promo

    async def record_redemption(
        self,
        promo_code_id: UUID,
        *,
        channel: str,
        email: str,
        discount_amount: Decimal,
        user_id: Optional[UUID] = None,
    ) -> None:
        await self.promo_repo.record_redemption(
            promo_code_id,
            channel=channel,
            email=email,
            discount_amount=discount_amount,
            user_id=user_id,
        )


def discount_price(promo: PromoCode, base: Decimal) -> Decimal:
    """The discounted unit price — never below zero. Applied per unit price
    (a ticket type's price, or the vote price) rather than to a cart total,
    so it composes cleanly with quantity for both checkout flows."""
    if promo.discount_type == "percentage":
        discount = base * promo.discount_value / Decimal("100")
    else:
        discount = promo.discount_value
    return max(Decimal("0"), (base - discount).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def _compute_status(promo: PromoCode) -> str:
    if promo.valid_until and promo.valid_until < date.today():
        return "expired"
    return "active" if promo.is_active else "inactive"
