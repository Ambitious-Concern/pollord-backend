import secrets
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException, status

from app.core.plans import TICKET_COMMISSION_RATE_BPS, commission_rate_bps
from app.models.election import Election
from app.models.event import Event
from app.models.org_activity_log import OrgActivityLog
from app.models.organization import Organization
from app.models.payout_request import PayoutRequest
from app.models.user import User
from app.repositories.election_repository import ElectionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.org_activity_log_repository import OrgActivityLogRepository
from app.repositories.organization_repository import OrganizationRepository
from app.repositories.payout_request_repository import PayoutRequestRepository
from app.repositories.ticket_repository import TicketPurchaseRepository
from app.repositories.transaction_repository import TransactionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.payout import (
    PayoutAvailableResponse,
    PayoutDestinationOption,
    PayoutRequestCreate,
    PayoutRequestResponse,
    PayoutSourceAvailability,
)
from app.services.org_notification_service import OrgNotificationService
from app.services.paystack_service import PaystackService

TWO_DP = Decimal("0.01")
# Published on the public Terms page (pricing-page.tsx / terms-page.tsx):
# "Withdrawals are subject to a minimum of GH₵50 and a maximum of GH₵10,000
# per transaction." Applies per payout request, not per source lifetime.
MIN_PAYOUT_GHS = Decimal("50.00")
MAX_PAYOUT_GHS = Decimal("10000.00")

# Paystack's recipient/bank-list `type` values for Ghana — "ghipss" for
# ordinary bank transfers, not "nuban" (that's Nigeria's).
_RECIPIENT_TYPE = {"mobile_money": "mobile_money", "bank_account": "ghipss"}


def _cedis(value: float) -> Decimal:
    """Currency amounts always display with 2 decimal places — a raw
    float->Decimal(str(...)) conversion drops trailing zeros (500.0 instead
    of 500.00), which reads like a formatting bug next to amounts sourced
    straight from a Numeric(10,2) column."""
    return Decimal(str(value)).quantize(TWO_DP)


class PayoutService:
    """Organizer payout requests (for both events and elections) and the
    admin review/pay-out flow, including the real Paystack Transfer call."""

    def __init__(
        self,
        payout_repo: PayoutRequestRepository,
        event_repo: EventRepository,
        purchase_repo: TicketPurchaseRepository,
        user_repo: UserRepository,
        election_repo: Optional[ElectionRepository] = None,
        transaction_repo: Optional[TransactionRepository] = None,
        paystack: Optional[PaystackService] = None,
    ):
        self.payout_repo = payout_repo
        self.event_repo = event_repo
        self.purchase_repo = purchase_repo
        self.user_repo = user_repo
        self.election_repo = election_repo
        self.transaction_repo = transaction_repo
        self.paystack = paystack

    @staticmethod
    def _to_response(
        req: PayoutRequest,
        event: Optional[Event],
        organizer: Optional[User],
        election: Optional[Election] = None,
    ) -> PayoutRequestResponse:
        return PayoutRequestResponse(
            payout_request_id=req.payout_request_id,
            event_id=req.event_id,
            event_title=event.title if event else "",
            election_id=req.election_id,
            election_title=election.title if election else "",
            organizer_id=req.organizer_id,
            organizer_name=organizer.full_name if organizer else "",
            organizer_email=organizer.email if organizer else "",
            amount=req.amount,
            status=req.status,
            express=req.express,
            admin_notes=req.admin_notes,
            payout_method=req.payout_method,
            recipient_name=req.recipient_name,
            destination_code=req.destination_code,
            destination_number=req.destination_number,
            transfer_reference=req.transfer_reference,
            transfer_status=req.transfer_status,
            requested_at=req.requested_at,
            reviewed_at=req.reviewed_at,
        )

    async def _require_event_and_ownership(self, event_id: UUID, user: User) -> Event:
        event = await self.event_repo.get_by_id(event_id, id_field="event_id")
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        is_admin = any(ur.role.role_name == "System Administrator" for ur in user.user_roles)
        if event.created_by != user.user_id and not is_admin:
            raise HTTPException(status_code=403, detail="You do not have access to this event")
        return event

    async def _require_election_and_ownership(self, election_id: UUID, user: User) -> Election:
        assert self.election_repo is not None
        election = await self.election_repo.get_by_id(election_id, id_field="election_id")
        if not election:
            raise HTTPException(status_code=404, detail="Election not found")
        is_admin = any(ur.role.role_name == "System Administrator" for ur in user.user_roles)
        if election.created_by != user.user_id and not is_admin:
            raise HTTPException(status_code=403, detail="You do not have access to this election")
        return election

    @staticmethod
    def _resolve_requested_amount(data: PayoutRequestCreate, available: Decimal) -> Decimal:
        """Defaults to the full available balance when the organizer doesn't
        specify an amount, otherwise enforces it's within what's available
        and within the published per-transaction min/max."""
        amount = data.amount if data.amount is not None else available
        if amount > available:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Only GH₵{available:,.2f} is available to request",
            )
        if amount < MIN_PAYOUT_GHS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"The minimum payout is GH₵{MIN_PAYOUT_GHS:,.2f}",
            )
        if amount > MAX_PAYOUT_GHS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"The maximum payout per request is GH₵{MAX_PAYOUT_GHS:,.2f}",
            )
        return amount.quantize(TWO_DP)

    @staticmethod
    def _ticket_platform_fee(event: Event, gross: Decimal) -> Decimal:
        bps = commission_rate_bps(
            event.plan, event.commission_rate_override_bps, rate_table=TICKET_COMMISSION_RATE_BPS
        )
        return (gross * bps / Decimal(10000)).quantize(Decimal("0.01"))

    @staticmethod
    def _vote_platform_fee(election: Election, gross: Decimal) -> Decimal:
        # commission_rate_bps defaults to the voting rate table (COMMISSION_RATE_BPS).
        bps = commission_rate_bps(election.plan, election.commission_rate_override_bps)
        return (gross * bps / Decimal(10000)).quantize(Decimal("0.01"))

    async def get_available(self, event_id: UUID, user: User) -> PayoutAvailableResponse:
        """Revenue collected minus Pollord's ticket commission, minus
        what's already been requested/paid."""
        event = await self._require_event_and_ownership(event_id, user)
        gross = _cedis(await self.purchase_repo.get_revenue_by_event(event_id))
        fee = self._ticket_platform_fee(event, gross)
        already_requested = _cedis(await self.payout_repo.get_total_requested(event_id=event_id))
        return PayoutAvailableResponse(
            event_id=event_id,
            gross_revenue=gross,
            platform_fee=fee,
            already_requested=already_requested,
            available=max(gross - fee - already_requested, Decimal("0")),
            has_pending_request=await self.payout_repo.has_pending(event_id=event_id),
        )

    async def _require_verified_organization(self, user: User, org_id: Optional[UUID]) -> None:
        """Identity verification gate — the one place KYC is actually enforced.

        Pollord deliberately does not ask for ID at signup or when creating an
        election/event: that requirement exists for AML/compliance reasons tied
        to moving money, so it belongs here, where real funds leave the
        platform. Everything before this call stays open to unverified
        organizers.

        Checks the organization that owns the payout source where there is
        one, rather than any org the requester happens to belong to —
        otherwise membership of somebody else's verified org would unlock
        payouts for your own unverified one. Elections and events can be
        personal (org_id NULL), and those fall back to the requester's own
        organizations, since verification is only ever recorded against an org.
        """
        repo = OrganizationRepository(Organization, self.payout_repo.session)

        if org_id is not None:
            org = await repo.get_by_id(org_id, id_field="org_id")
            if org is not None and org.is_verified:
                return
        else:
            orgs = await repo.get_user_organizations(user.user_id)
            if any(o.is_verified for o in orgs):
                return

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Your organization must be verified before you can request a payout. "
                "Upload your ID in Settings → Verification and we'll review it shortly."
            ),
        )

    async def request_payout(
        self,
        event_id: UUID,
        user: User,
        data: PayoutRequestCreate,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> PayoutRequestResponse:
        event = await self._require_event_and_ownership(event_id, user)
        await self._require_verified_organization(user, event.org_id)

        if await self.payout_repo.has_pending(event_id=event_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="There is already a pending payout request for this event",
            )

        gross = _cedis(await self.purchase_repo.get_revenue_by_event(event_id))
        fee = self._ticket_platform_fee(event, gross)
        already_requested = _cedis(await self.payout_repo.get_total_requested(event_id=event_id))
        available = gross - fee - already_requested
        if available <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No revenue available to request a payout for",
            )
        amount = self._resolve_requested_amount(data, available)

        req = await self.payout_repo.create(
            {
                "event_id": event_id,
                "organizer_id": user.user_id,
                "amount": amount,
                "status": "pending",
                "express": True,  # express payout is open to every plan
                "payout_method": data.payout_method,
                "recipient_name": data.recipient_name,
                "destination_code": data.destination_code,
                "destination_number": data.destination_number,
            }
        )
        if event.org_id is not None:
            await OrgActivityLogRepository(OrgActivityLog, self.payout_repo.session).log_activity(
                org_id=event.org_id,
                category="payout",
                verb="requested a payout from",
                target_name=event.title,
                user_id=user.user_id,
                detail=f"GH₵{amount:,.2f}",
                ip_address=ip,
                user_agent=user_agent,
            )
        return self._to_response(req, event, user)

    async def list_for_event(self, event_id: UUID, user: User) -> List[PayoutRequestResponse]:
        event = await self._require_event_and_ownership(event_id, user)
        requests = await self.payout_repo.get_by_parent(event_id=event_id)
        return [self._to_response(r, event, user) for r in requests]

    # --- Election-scoped mirror of the event methods above. Kept as separate
    # methods rather than threading a parent_kind flag through the event ones
    # so a bug in the newer election path can't touch the already-live,
    # revenue-critical event path. ---

    async def get_available_for_election(
        self, election_id: UUID, user: User
    ) -> PayoutAvailableResponse:
        assert self.transaction_repo is not None
        election = await self._require_election_and_ownership(election_id, user)
        gross = _cedis(await self.transaction_repo.get_revenue_by_election(election_id))
        fee = self._vote_platform_fee(election, gross)
        already_requested = _cedis(
            await self.payout_repo.get_total_requested(election_id=election_id)
        )
        return PayoutAvailableResponse(
            election_id=election_id,
            gross_revenue=gross,
            platform_fee=fee,
            already_requested=already_requested,
            available=max(gross - fee - already_requested, Decimal("0")),
            has_pending_request=await self.payout_repo.has_pending(election_id=election_id),
        )

    async def request_payout_for_election(
        self,
        election_id: UUID,
        user: User,
        data: PayoutRequestCreate,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> PayoutRequestResponse:
        assert self.transaction_repo is not None
        election = await self._require_election_and_ownership(election_id, user)
        await self._require_verified_organization(user, election.org_id)

        if await self.payout_repo.has_pending(election_id=election_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="There is already a pending payout request for this election",
            )

        gross = _cedis(await self.transaction_repo.get_revenue_by_election(election_id))
        fee = self._vote_platform_fee(election, gross)
        already_requested = _cedis(
            await self.payout_repo.get_total_requested(election_id=election_id)
        )
        available = gross - fee - already_requested
        if available <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No revenue available to request a payout for",
            )
        amount = self._resolve_requested_amount(data, available)

        req = await self.payout_repo.create(
            {
                "election_id": election_id,
                "organizer_id": user.user_id,
                "amount": amount,
                "status": "pending",
                "express": True,  # express payout is open to every plan
                "payout_method": data.payout_method,
                "recipient_name": data.recipient_name,
                "destination_code": data.destination_code,
                "destination_number": data.destination_number,
            }
        )
        if election.org_id is not None:
            await OrgActivityLogRepository(OrgActivityLog, self.payout_repo.session).log_activity(
                org_id=election.org_id,
                category="payout",
                verb="requested a payout from",
                target_name=election.title,
                user_id=user.user_id,
                detail=f"GH₵{amount:,.2f}",
                ip_address=ip,
                user_agent=user_agent,
            )
            member_ids = await OrganizationRepository(Organization, self.payout_repo.session).get_member_ids(
                election.org_id
            )
            await OrgNotificationService(self.payout_repo.session).notify(
                org_id=election.org_id,
                member_ids=member_ids,
                pref_key="payout_requested",
                notif_type="payout",
                title=f"Payout requested from {election.title}",
                description=f"GH₵{amount:,.2f} requested.",
                link="/dashboard/insights/payout",
            )
        return self._to_response(req, None, user, election)

    async def list_for_election(
        self, election_id: UUID, user: User
    ) -> List[PayoutRequestResponse]:
        election = await self._require_election_and_ownership(election_id, user)
        requests = await self.payout_repo.get_by_parent(election_id=election_id)
        return [self._to_response(r, None, user, election) for r in requests]

    async def list_available_sources(self, user: User) -> List[PayoutSourceAvailability]:
        """Every election/event the caller's team has ever earned money from,
        with its payable balance — the bulk equivalent of get_available /
        get_available_for_election for the sources table. Uses grouped
        queries throughout instead of looping per-source, since a single
        organizer can own dozens of elections and events."""
        assert self.transaction_repo is not None and self.election_repo is not None
        teammate_ids = await OrganizationRepository(
            Organization, self.payout_repo.session
        ).get_teammate_ids(user.user_id)

        elections = await self.election_repo.get_elections_by_creators(teammate_ids, limit=10000)
        events = await self.event_repo.get_events_by_creators(teammate_ids, limit=10000)
        election_ids = [e.election_id for e in elections]
        event_ids = [e.event_id for e in events]

        election_revenue = await self.transaction_repo.get_revenue_by_elections(election_ids)
        event_revenue = await self.purchase_repo.get_revenue_by_events(event_ids)
        requested_by_event, requested_by_election, pending_events, pending_elections = (
            await self.payout_repo.get_totals_by_parents(event_ids=event_ids, election_ids=election_ids)
        )

        sources: List[PayoutSourceAvailability] = []
        for election in elections:
            gross = _cedis(election_revenue.get(election.election_id, 0))
            if gross <= 0:
                continue
            fee = self._vote_platform_fee(election, gross)
            already_requested = _cedis(requested_by_election.get(election.election_id, 0))
            available = max(gross - fee - already_requested, Decimal("0"))
            has_pending = election.election_id in pending_elections
            # Fully paid out and nothing pending — its story is already told
            # in Payout history, so it drops out of the actionable sources
            # list rather than sitting there with a permanently disabled row.
            if available <= 0 and not has_pending:
                continue
            sources.append(
                PayoutSourceAvailability(
                    source_id=election.election_id,
                    source_type="election",
                    source_name=election.title,
                    gross_revenue=gross,
                    platform_fee=fee,
                    already_requested=already_requested,
                    available=available,
                    has_pending_request=has_pending,
                )
            )
        for event in events:
            gross = _cedis(event_revenue.get(event.event_id, 0))
            if gross <= 0:
                continue
            fee = self._ticket_platform_fee(event, gross)
            already_requested = _cedis(requested_by_event.get(event.event_id, 0))
            available = max(gross - fee - already_requested, Decimal("0"))
            has_pending = event.event_id in pending_events
            if available <= 0 and not has_pending:
                continue
            sources.append(
                PayoutSourceAvailability(
                    source_id=event.event_id,
                    source_type="event",
                    source_name=event.title,
                    gross_revenue=gross,
                    platform_fee=fee,
                    already_requested=already_requested,
                    available=available,
                    has_pending_request=has_pending,
                )
            )
        return sources

    async def list_mine(self, organizer_id: UUID) -> List[PayoutRequestResponse]:
        requests = await self.payout_repo.get_by_organizer(organizer_id)
        # get_by_organizer eager-loads `event`/`election`; organizer is always the caller.
        organizer = await self.user_repo.get_by_id(organizer_id, id_field="user_id")
        return [self._to_response(r, r.event, organizer, r.election) for r in requests]

    async def list_all(self, status_filter: Optional[str] = None) -> List[PayoutRequestResponse]:
        requests = await self.payout_repo.get_all(status_filter)
        # get_all eager-loads `event`, `election`, and `organizer`.
        return [self._to_response(r, r.event, r.organizer, r.election) for r in requests]

    async def review(
        self, payout_request_id: UUID, new_status: str, admin_notes: Optional[str], admin: User
    ) -> PayoutRequestResponse:
        """Admin approve/reject without an automatic transfer — for
        marking something paid manually. See initiate_transfer for the
        actual Paystack payout path."""
        if new_status not in ("paid", "rejected"):
            raise HTTPException(status_code=400, detail="status must be 'paid' or 'rejected'")
        req = await self.payout_repo.mark_reviewed(
            payout_request_id, new_status, admin_notes, admin.user_id
        )
        if not req:
            raise HTTPException(status_code=404, detail="Payout request not found")
        event = (
            await self.event_repo.get_by_id(req.event_id, id_field="event_id")
            if req.event_id
            else None
        )
        election = (
            await self.election_repo.get_by_id(req.election_id, id_field="election_id")
            if req.election_id and self.election_repo
            else None
        )
        parent_title = event.title if event else (election.title if election else None)
        parent_org_id = event.org_id if event else (election.org_id if election else None)
        if parent_org_id is not None:
            member_ids = await OrganizationRepository(Organization, self.payout_repo.session).get_member_ids(
                parent_org_id
            )
            if new_status == "paid":
                await OrgNotificationService(self.payout_repo.session).notify(
                    org_id=parent_org_id,
                    member_ids=member_ids,
                    pref_key="payout_paid",
                    notif_type="payout",
                    title=f"Payout of GH₵{req.amount:,.2f} processed",
                    description=f"Marked paid manually for {parent_title}.",
                    link="/dashboard/insights/payout",
                )
            else:
                await OrgNotificationService(self.payout_repo.session).notify(
                    org_id=parent_org_id,
                    member_ids=member_ids,
                    pref_key="payout_rejected",
                    notif_type="payout",
                    title=f"Payout of GH₵{req.amount:,.2f} rejected",
                    description=admin_notes or f"Needs corrected details for {parent_title}.",
                    link="/dashboard/insights/payout",
                )

        organizer = await self.user_repo.get_by_id(req.organizer_id, id_field="user_id")
        return self._to_response(req, event, organizer, election)

    async def list_mobile_money_networks(self) -> List[PayoutDestinationOption]:
        """Fetched live from Paystack rather than hardcoded — a wrong network
        code here means a transfer could go to the wrong destination."""
        assert self.paystack is not None
        banks = await self.paystack.list_banks(currency="GHS", transfer_type="mobile_money")
        return [PayoutDestinationOption(name=b["name"], code=b["code"]) for b in banks]

    async def list_banks(self) -> List[PayoutDestinationOption]:
        """Same as list_mobile_money_networks but for ordinary Ghanaian bank
        accounts — Paystack's "ghipss" type, not "mobile_money"."""
        assert self.paystack is not None
        banks = await self.paystack.list_banks(currency="GHS", transfer_type="ghipss")
        return [PayoutDestinationOption(name=b["name"], code=b["code"]) for b in banks]

    async def initiate_transfer(
        self,
        payout_request_id: UUID,
        admin: User,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> PayoutRequestResponse:
        """Pays a pending request out via Paystack Transfer, called from the
        admin console. Only flips status to "paid" if Paystack confirms the
        transfer actually succeeded — see record_transfer_result."""
        assert self.paystack is not None
        req = await self.payout_repo.get_by_id(payout_request_id, id_field="payout_request_id")
        if not req:
            raise HTTPException(status_code=404, detail="Payout request not found")
        if req.status != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"This request is already {req.status}, nothing to pay",
            )
        if not (req.destination_code and req.destination_number and req.recipient_name):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "This request has no payout destination on file (it may predate "
                    "this feature). Pay the organizer manually and mark it paid instead."
                ),
            )
        recipient_type = _RECIPIENT_TYPE.get(req.payout_method or "mobile_money", "mobile_money")

        # Confirm the number actually resolves to a real account before we
        # ever create a recipient or move money against it.
        resolved = await self.paystack.resolve_account(req.destination_number, req.destination_code)

        recipient_code = req.paystack_recipient_code
        if not recipient_code:
            recipient = await self.paystack.create_transfer_recipient(
                name=resolved.get("account_name") or req.recipient_name,
                account_number=req.destination_number,
                bank_code=req.destination_code,
                currency="GHS",
                recipient_type=recipient_type,
            )
            recipient_code = recipient["recipient_code"]
            await self.payout_repo.set_recipient_code(payout_request_id, recipient_code)

        event = (
            await self.event_repo.get_by_id(req.event_id, id_field="event_id")
            if req.event_id
            else None
        )
        election = (
            await self.election_repo.get_by_id(req.election_id, id_field="election_id")
            if req.election_id and self.election_repo
            else None
        )
        parent_title = event.title if event else election.title if election else (req.event_id or req.election_id)
        reference = f"payout_{secrets.token_urlsafe(16)}"
        transfer = await self.paystack.initiate_transfer(
            amount=int(req.amount * 100),  # pesewas
            recipient_code=recipient_code,
            reason=f"Pollord payout: {parent_title}",
            reference=reference,
        )

        # From here on, Paystack has already accepted (or queued, or
        # rejected) the transfer — a real-world action has happened. This
        # DB write recording that outcome must land no matter what, so we
        # deliberately never raise past this point: get_db() rolls back the
        # session on any exception, and rolling back here would make the
        # request look untouched — retrying would call Paystack a second
        # time for money that may already be moving. Callers read
        # `transfer_status` in the response body instead of an HTTP error.
        transfer_status = transfer.get("status", "unknown")
        updated = await self.payout_repo.record_transfer_result(
            payout_request_id,
            transfer_reference=reference,
            transfer_status=transfer_status,
            mark_paid=(transfer_status == "success"),
            reviewed_by=admin.user_id,
        )
        if not updated:
            # Should be unreachable (we already loaded `req` above), but
            # fall back to the pre-update object rather than raise.
            updated = req

        parent_org_id = event.org_id if event else (election.org_id if election else None)
        if transfer_status == "success" and parent_org_id is not None:
            await OrgActivityLogRepository(OrgActivityLog, self.payout_repo.session).log_activity(
                org_id=parent_org_id,
                category="payout",
                verb="paid out",
                target_name=str(parent_title),
                user_id=admin.user_id,
                ip_address=ip,
                user_agent=user_agent,
                detail=f"GH₵{req.amount:,.2f} via mobile money",
            )
            member_ids = await OrganizationRepository(Organization, self.payout_repo.session).get_member_ids(
                parent_org_id
            )
            await OrgNotificationService(self.payout_repo.session).notify(
                org_id=parent_org_id,
                member_ids=member_ids,
                pref_key="payout_paid",
                notif_type="payout",
                title=f"Payout of GH₵{req.amount:,.2f} processed",
                description="Sent to your mobile money account.",
                link="/dashboard/insights/payout",
            )

        organizer = await self.user_repo.get_by_id(req.organizer_id, id_field="user_id")
        return self._to_response(updated, event, organizer, election)
