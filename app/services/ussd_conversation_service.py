"""
USSD conversation state machine (Arkesel gateway).

States (stored per phone number in Redis, TTL = USSD_SESSION_TTL_SECONDS —
matches Arkesel's own ~180s session timeout):
  idle              → greet, ask for the election/event's USSD code
  awaiting_category → parent has >1 category with candidates, waiting for a number
  awaiting_vote     → ballot shown, waiting for one candidate short code
  awaiting_network  → paid vote — waiting for a mobile money network choice

Voter identity: phone number is HMAC-hashed per category — never stored in
plaintext, same convention as generate_whatsapp_voter_hash.

Payment: unlike the web/WhatsApp redirect flow, USSD has no browser to send a
link to — a direct Paystack mobile money charge is initiated against the same
phone number that dialed in, and the session ends immediately with
instructions to approve on-phone. Completion is always reported later via the
Paystack webhook (payments.py), which sends the confirmation as an SMS
through Arkesel instead of ending the (already-closed) USSD session.
"""

import hashlib
import json
import logging
import secrets as _secrets
import uuid as _uuid
from datetime import datetime, timezone
from typing import Optional, Tuple
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.endpoints.voting import (
    _assert_open_for_voting,
    _get_effective_vote_price,
)
from app.core.config import settings
from app.core.security import generate_ussd_voter_hash
from app.models.audit_log import AuditLog
from app.models.election import Election
from app.models.event import Event
from app.models.transaction import Transaction
from app.models.vote import Vote
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.election_repository import ElectionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.transaction_repository import TransactionRepository
from app.repositories.vote_repository import VoteRepository
from app.services.cryptography_service import CryptographyService
from app.services.paystack_service import PaystackService
from app.services.voting_service import resolve_candidate_selection

logger = logging.getLogger(__name__)

SESSION_TTL = settings.USSD_SESSION_TTL_SECONDS
PAYMENT_TTL = 3600  # 1 hr for pending payment data in Redis

_CURRENCY_SYMBOL = {"GHS": "GHS ", "NGN": "NGN ", "USD": "$", "EUR": "EUR "}

# Paystack's Ghana mobile_money provider identifiers.
_NETWORKS = {
    "1": ("mtn", "MTN Mobile Money"),
    "2": ("vod", "Vodafone Cash"),
    "3": ("atl", "AirtelTigo Money"),
}


class UssdConversationService:
    def __init__(self, db: AsyncSession, redis: aioredis.Redis):
        self.db = db
        self.redis = redis
        self.crypto = CryptographyService()
        self.election_repo = ElectionRepository(Election, db)
        self.event_repo = EventRepository(Event, db)
        self.vote_repo = VoteRepository(Vote, db)
        self.txn_repo = TransactionRepository(db)
        self.audit_repo = AuditLogRepository(AuditLog, db)

    # ------------------------------------------------------------------
    # Public entry point — returns (message, end_session)
    # ------------------------------------------------------------------

    async def handle(self, phone: str, text: str) -> Tuple[str, bool]:
        """Entry point called once per USSD request: dispatches on the
        caller's stored state to the right handler. Returns (message,
        end_session)."""
        text = text.strip()
        session = await self._get_session(phone)
        state = session.get("state", "idle")

        if state == "awaiting_category":
            return await self._handle_category_selection(phone, text, session)
        if state == "awaiting_vote":
            return await self._handle_vote_input(phone, text, session)
        if state == "awaiting_network":
            return await self._handle_network_selection(phone, text, session)

        return await self._handle_code_lookup(phone, text)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_code_lookup(self, phone: str, text: str) -> Tuple[str, bool]:
        code = "".join(ch for ch in text if ch.isdigit())
        if not code:
            return (
                "Welcome to Pollord Voting.\nEnter the election/event code to vote:",
                False,
            )

        parent, parent_kind = await self._resolve_parent(code)
        if not parent:
            return ("Code not found. Please check and try again:", False)

        try:
            _assert_open_for_voting(parent, parent_kind)
        except HTTPException as exc:
            return (str(exc.detail), True)

        categories = sorted(parent.categories, key=lambda c: c.display_order)
        categories = [c for c in categories if c.candidates]
        if not categories:
            return (f"{parent.title} has no candidates yet.", True)

        if len(categories) == 1:
            return await self._present_ballot(phone, parent, parent_kind, categories[0])

        lines = [f"{parent.title}", "Pick a category:"]
        for i, cat in enumerate(categories, start=1):
            lines.append(f"{i}. {cat.name}")

        await self._set_session(
            phone,
            {
                "state": "awaiting_category",
                "parent_id": str(_parent_id(parent, parent_kind)),
                "parent_kind": parent_kind,
                "category_ids": [str(c.category_id) for c in categories],
            },
        )
        return ("\n".join(lines), False)

    async def _handle_category_selection(
        self, phone: str, text: str, session: dict
    ) -> Tuple[str, bool]:
        category_ids = session.get("category_ids", [])
        choice = text.strip()
        if not choice.isdigit() or not (1 <= int(choice) <= len(category_ids)):
            return (f"Reply with a number from 1 to {len(category_ids)}:", False)

        parent, parent_kind = await self._reload_parent(session)
        if not parent:
            await self._clear_session(phone)
            return ("No longer available. Dial in again to retry.", True)

        category_id = UUID(category_ids[int(choice) - 1])
        category = next(
            (c for c in parent.categories if c.category_id == category_id), None
        )
        if not category:
            await self._clear_session(phone)
            return ("That category is no longer available.", True)

        return await self._present_ballot(phone, parent, parent_kind, category)

    async def _present_ballot(
        self, phone: str, parent, parent_kind: str, category
    ) -> Tuple[str, bool]:
        if category.election_type != "single_choice":
            return (
                f"{category.name} needs a ranked ballot — please vote for it "
                "on the website or via WhatsApp instead.",
                True,
            )

        allow_revoting = getattr(parent, "allow_revoting", False)
        if not allow_revoting:
            voter_hash = generate_ussd_voter_hash(phone, category.category_id)
            if await self.vote_repo.has_voted(voter_hash, category.category_id):
                return (f"You have already voted in {category.name}.", True)

        candidates = sorted(category.candidates, key=lambda c: c.display_order)
        vote_price = await _get_effective_vote_price(parent, self.db)

        lines = [f"{parent.title}: {category.name}", "Enter a candidate code:"]
        for c in candidates:
            if c.short_code:
                lines.append(f"{c.short_code}: {c.name}")

        if vote_price > 0:
            symbol = _CURRENCY_SYMBOL.get(
                settings.VOTE_CURRENCY, settings.VOTE_CURRENCY
            )
            lines.append(f"Cost: {symbol}{vote_price / 100:.2f}")

        await self._set_session(
            phone,
            {
                "state": "awaiting_vote",
                "parent_id": str(_parent_id(parent, parent_kind)),
                "parent_kind": parent_kind,
                "category_id": str(category.category_id),
                "vote_price": vote_price,
            },
        )
        return ("\n".join(lines), False)

    async def _handle_vote_input(
        self, phone: str, text: str, session: dict
    ) -> Tuple[str, bool]:
        parent, parent_kind = await self._reload_parent(session)
        if not parent:
            await self._clear_session(phone)
            return "No longer available. Dial in again to retry.", True

        try:
            _assert_open_for_voting(parent, parent_kind)
        except HTTPException as exc:
            await self._clear_session(phone)
            return (str(exc.detail), True)

        category_id = UUID(session["category_id"])
        category = next(
            (c for c in parent.categories if c.category_id == category_id), None
        )
        if not category:
            await self._clear_session(phone)
            return ("That category is no longer available.", True)

        code = text.strip().upper()
        if not code:
            return ("Please enter a candidate code:", False)

        try:
            candidate_ids = resolve_candidate_selection(category, None, [code])
        except HTTPException as exc:
            return (f"{exc.detail}\nTry again or dial in fresh to restart.", False)

        vote_price = session.get("vote_price", 0)
        allow_revoting = getattr(parent, "allow_revoting", False)
        base_hash = generate_ussd_voter_hash(phone, category_id)
        if allow_revoting:
            voter_hash = hashlib.sha256(
                f"{base_hash}:{_uuid.uuid4().hex}".encode()
            ).hexdigest()
        else:
            voter_hash = base_hash
            if await self.vote_repo.has_voted(voter_hash, category_id):
                await self._clear_session(phone)
                return (f"You have already voted in {category.name}.", True)

        candidate = next(
            c for c in category.candidates if c.candidate_id == candidate_ids[0]
        )

        if vote_price == 0:
            return await self._cast_vote_directly(
                phone, parent, parent_kind, category, candidate, voter_hash
            )

        await self._set_session(
            phone,
            {
                "state": "awaiting_network",
                "parent_id": str(_parent_id(parent, parent_kind)),
                "parent_kind": parent_kind,
                "category_id": str(category_id),
                "candidate_id": str(candidate.candidate_id),
                "candidate_name": candidate.name,
                "voter_hash": voter_hash,
                "vote_price": vote_price,
            },
        )
        lines = [f"Vote for {candidate.name}. Choose payment network:"]
        for key, (_, label) in _NETWORKS.items():
            lines.append(f"{key}. {label}")
        return ("\n".join(lines), False)

    async def _handle_network_selection(
        self, phone: str, text: str, session: dict
    ) -> Tuple[str, bool]:
        choice = text.strip()
        network = _NETWORKS.get(choice)
        if not network:
            return ("Reply 1 for MTN, 2 for Vodafone, or 3 for AirtelTigo:", False)

        parent, parent_kind = await self._reload_parent(session)
        if not parent:
            await self._clear_session(phone)
            return ("No longer available. Dial in again to retry.", True)

        provider, provider_label = network
        category_id = UUID(session["category_id"])
        candidate_id = UUID(session["candidate_id"])
        candidate_name = session.get("candidate_name", "")
        voter_hash = session["voter_hash"]
        vote_price = session.get("vote_price", 0)

        reference = f"vote_ussd_{_secrets.token_urlsafe(16)}"
        phone_hash = hashlib.sha256(phone.encode()).hexdigest()[:12]
        placeholder_email = f"ussd{phone_hash}@pollord.vote"

        await self.txn_repo.create(
            {
                "reference": reference,
                "election_id": (
                    UUID(session["parent_id"]) if parent_kind == "election" else None
                ),
                "event_id": (
                    UUID(session["parent_id"]) if parent_kind == "event" else None
                ),
                "category_id": category_id,
                "voter_hash": voter_hash,
                "email": placeholder_email,
                "candidate_ids": [str(candidate_id)],
                "amount": vote_price,
                "currency": settings.VOTE_CURRENCY,
                "status": "pending",
            }
        )

        paystack = PaystackService(settings.PAYSTACK_SECRET_KEY)
        try:
            await paystack.charge_mobile_money(
                email=placeholder_email,
                amount=vote_price,
                reference=reference,
                phone=phone,
                provider=provider,
                currency=settings.VOTE_CURRENCY,
                metadata={
                    "parent_kind": parent_kind,
                    "parent_id": session["parent_id"],
                    "category_id": str(category_id),
                    "channel": "ussd",
                },
            )
        except HTTPException as exc:
            await self.txn_repo.update_status(reference, "failed")
            await self._clear_session(phone)
            return (f"Payment could not be started: {exc.detail}", True)

        # So the Paystack webhook (payments.py) knows to text this phone.
        await self.redis.setex(
            f"ussd:payment:{reference}",
            PAYMENT_TTL,
            json.dumps({"phone": phone}),
        )
        await self._clear_session(phone)

        symbol = _CURRENCY_SYMBOL.get(settings.VOTE_CURRENCY, settings.VOTE_CURRENCY)
        amount_display = f"{symbol}{vote_price / 100:.2f}"
        return (
            f"Check your phone and approve the {provider_label} prompt for "
            f"{amount_display} to vote for {candidate_name}. "
            "You'll get an SMS once it's confirmed.",
            True,
        )

    async def _cast_vote_directly(
        self, phone: str, parent, parent_kind: str, category, candidate, voter_hash: str
    ) -> Tuple[str, bool]:
        now = datetime.now(timezone.utc)
        encrypted = self.crypto.encrypt_vote_data([str(candidate.candidate_id)])
        cast_at = now.isoformat()
        signature = self.crypto.sign_vote(encrypted, cast_at)

        await self.vote_repo.create(
            {
                "category_id": category.category_id,
                "election_id": (
                    parent.election_id if parent_kind == "election" else None
                ),
                "event_id": parent.event_id if parent_kind == "event" else None,
                "voter_hash": voter_hash,
                "vote_data": encrypted,
                "vote_signature": signature,
                "count": 1,
            }
        )

        receipt_code = self.crypto.generate_receipt_code()

        await self.audit_repo.log_action(
            action_type="VOTE_CAST",
            entity_type=parent_kind.title(),
            entity_id=_parent_id(parent, parent_kind),
            user_id=None,
            ip_address="ussd",
            user_agent=f"USSD:{phone[:4]}****",
        )

        await self._clear_session(phone)

        return (
            f"Vote cast for {candidate.name} in {category.name}. "
            f"Receipt: {receipt_code[:12]}. Thank you!",
            True,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _resolve_parent(self, code: str):
        election = await self.election_repo.get_by_ussd_code(code)
        if election:
            return election, "election"
        event = await self.event_repo.get_by_ussd_code(code)
        if event:
            return event, "event"
        return None, ""

    async def _reload_parent(self, session: dict):
        parent_kind = session.get("parent_kind", "")
        parent_id = session.get("parent_id")
        if not parent_id:
            return None, parent_kind
        if parent_kind == "election":
            return (
                await self.election_repo.get_with_categories(UUID(parent_id)),
                parent_kind,
            )
        return await self.event_repo.get_with_categories(UUID(parent_id)), parent_kind

    def _key(self, phone: str) -> str:
        return f"ussd:session:{phone}"

    async def _get_session(self, phone: str) -> dict:
        raw = await self.redis.get(self._key(phone))
        if raw:
            return json.loads(raw)
        return {"state": "idle"}

    async def _set_session(self, phone: str, data: dict) -> None:
        await self.redis.setex(self._key(phone), SESSION_TTL, json.dumps(data))

    async def _clear_session(self, phone: str) -> None:
        await self.redis.delete(self._key(phone))


def _parent_id(parent, parent_kind: str) -> Optional[UUID]:
    return parent.election_id if parent_kind == "election" else parent.event_id
