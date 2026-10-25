"""USSD conversation state machine (Arkesel gateway).

States: idle -> awaiting_vote -> awaiting_vote_count -> awaiting_network ->
awaiting_checkout_confirm. Free votes skip straight to casting; network is
skipped if _detect_network guesses it from the phone prefix. Payment is a
direct JuniPay mobile money charge against the dialing number (no browser to
redirect); final confirmation always arrives later via the JuniPay webhook
(junipay_webhook.py), sent as an SMS since the USSD session is already
closed by then.
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.results_stream import notify_vote_cast
from app.services.org_notification_service import notify_vote_cast_if_due
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
from app.services.junipay_service import JunipayService

logger = logging.getLogger(__name__)

SESSION_TTL = settings.USSD_SESSION_TTL_SECONDS
PAYMENT_TTL = 3600  # 1 hr for pending payment data in Redis

_CURRENCY_SYMBOL = {"GHS": "GHS ", "NGN": "NGN ", "USD": "$", "EUR": "EUR "}

# Our own network identifiers — mapped to each gateway's own provider
# strings inside the service that actually calls it (see JunipayService).
_NETWORKS = {
    "1": ("mtn", "MTN Mobile Money"),
    "2": ("vod", "Vodafone Cash"),
    "3": ("atl", "AirtelTigo Money"),
}

# Original Ghana number-block allocations — best-effort guess to skip the
# network menu; a ported number just fails the charge with a clear error.
_NETWORK_PREFIXES = {
    "24": ("mtn", "MTN Mobile Money"),
    "25": ("mtn", "MTN Mobile Money"),
    "53": ("mtn", "MTN Mobile Money"),
    "54": ("mtn", "MTN Mobile Money"),
    "55": ("mtn", "MTN Mobile Money"),
    "59": ("mtn", "MTN Mobile Money"),
    "20": ("vod", "Vodafone Cash"),
    "50": ("vod", "Vodafone Cash"),
    "26": ("atl", "AirtelTigo Money"),
    "27": ("atl", "AirtelTigo Money"),
    "56": ("atl", "AirtelTigo Money"),
    "57": ("atl", "AirtelTigo Money"),
}

MAX_USSD_VOTE_QUANTITY = 100


def _join_sentence(first: str, second: str) -> str:
    """Join two sentence fragments without a doubled period."""
    return f"{first.rstrip('. ')}. {second}"


def _detect_network(phone: str) -> Optional[Tuple[str, str]]:
    """Best-guess (provider, label) from a Ghana MSISDN's block allocation,
    or None if unrecognized. Accepts 233XXXXXXXXX, 0XXXXXXXXX, or XXXXXXXXX."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if digits.startswith("233"):
        digits = "0" + digits[3:]
    if not digits.startswith("0"):
        digits = "0" + digits
    return _NETWORK_PREFIXES.get(digits[1:3])


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

        if state == "awaiting_vote":
            return await self._handle_vote_input(phone, text, session)
        if state == "awaiting_vote_count":
            return await self._handle_vote_count(phone, text, session)
        if state == "awaiting_network":
            return await self._handle_network_selection(phone, text, session)
        if state == "awaiting_checkout_confirm":
            return await self._handle_checkout_confirm(phone, text, session)

        return await self._handle_code_lookup(phone, text)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_code_lookup(self, phone: str, text: str) -> Tuple[str, bool]:
        code = "".join(ch for ch in text if ch.isdigit())
        if not code:
            return (
                "Welcome to Pollord Platform.\nEnter the election/event code to vote:",
                False,
            )

        parent, parent_kind = await self._resolve_parent(code)
        if not parent:
            return "Code not found. Please check and try again:", False

        try:
            _assert_open_for_voting(parent, parent_kind)
        except HTTPException as exc:
            return (str(exc.detail), True)

        has_candidates = any(c.candidates for c in parent.categories)
        if not has_candidates:
            return (f"{parent.title} has no candidates yet.", True)

        vote_price = await _get_effective_vote_price(parent, self.db)
        lines = [parent.title, "Enter your nominee's code:"]
        if vote_price > 0:
            symbol = _CURRENCY_SYMBOL.get(
                settings.VOTE_CURRENCY, settings.VOTE_CURRENCY
            )
            lines.append(f"Cost: {symbol}{vote_price / 100:.2f}/vote")

        await self._set_session(
            phone,
            {
                "state": "awaiting_vote",
                "parent_id": str(_parent_id(parent, parent_kind)),
                "parent_kind": parent_kind,
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

        code = text.strip().upper()
        if not code:
            return ("Please enter your nominee's code:", False)

        category, candidate = _find_candidate_by_code(parent, code)
        if not candidate:
            return (f"Unknown nominee code: {code}. Try again:", False)

        if category.election_type != "single_choice":
            await self._clear_session(phone)
            return (
                f"{category.name} needs a ranked ballot — please vote for it "
                "on the website or via WhatsApp instead.",
                True,
            )

        vote_price = session.get("vote_price", 0)
        allow_revoting = getattr(parent, "allow_revoting", False)
        base_hash = generate_ussd_voter_hash(phone, category.category_id)
        if allow_revoting:
            voter_hash = hashlib.sha256(
                f"{base_hash}:{_uuid.uuid4().hex}".encode()
            ).hexdigest()
        else:
            voter_hash = base_hash
            if await self.vote_repo.has_voted(voter_hash, category.category_id):
                await self._clear_session(phone)
                return (f"You have already voted in {category.name}.", True)

        if vote_price == 0:
            return await self._cast_vote_directly(
                phone, parent, parent_kind, category, candidate, voter_hash
            )

        await self._set_session(
            phone,
            {
                "state": "awaiting_vote_count",
                "parent_id": str(_parent_id(parent, parent_kind)),
                "parent_kind": parent_kind,
                "category_id": str(category.category_id),
                "candidate_id": str(candidate.candidate_id),
                "candidate_name": candidate.name,
                "voter_hash": voter_hash,
                "vote_price": vote_price,
            },
        )
        symbol = _CURRENCY_SYMBOL.get(settings.VOTE_CURRENCY, settings.VOTE_CURRENCY)
        return (
            f"{candidate.name} ({category.name}) - {symbol}{vote_price / 100:.2f}/vote.\n"
            f"How many votes? (1-{MAX_USSD_VOTE_QUANTITY}):",
            False,
        )

    async def _handle_vote_count(
        self, phone: str, text: str, session: dict
    ) -> Tuple[str, bool]:
        choice = text.strip()
        if not choice.isdigit() or not (1 <= int(choice) <= MAX_USSD_VOTE_QUANTITY):
            return (
                f"Enter a number from 1 to {MAX_USSD_VOTE_QUANTITY}:",
                False,
            )

        vote_count = int(choice)
        vote_price = session.get("vote_price", 0)
        candidate_name = session.get("candidate_name", "")
        total_amount = vote_price * vote_count

        updated_session = {
            **session,
            "state": "awaiting_network",
            "vote_count": vote_count,
            "amount": total_amount,
        }

        detected = _detect_network(phone)
        if detected:
            provider, provider_label = detected
            return await self._start_checkout_confirm(
                phone, updated_session, provider, provider_label
            )

        await self._set_session(phone, updated_session)
        symbol = _CURRENCY_SYMBOL.get(settings.VOTE_CURRENCY, settings.VOTE_CURRENCY)
        lines = [
            f"{vote_count} vote(s) for {candidate_name}: {symbol}{total_amount / 100:.2f}. "
            "Choose payment network:"
        ]
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

        provider, provider_label = network
        return await self._start_checkout_confirm(
            phone, session, provider, provider_label
        )

    async def _start_checkout_confirm(
        self, phone: str, session: dict, provider: str, provider_label: str
    ) -> Tuple[str, bool]:
        """Last confirm before handing off to JuniPay — nothing charged yet."""
        await self._set_session(
            phone,
            {
                **session,
                "state": "awaiting_checkout_confirm",
                "provider": provider,
                "provider_label": provider_label,
            },
        )
        symbol = _CURRENCY_SYMBOL.get(settings.VOTE_CURRENCY, settings.VOTE_CURRENCY)
        vote_count = session.get("vote_count", 1)
        candidate_name = session.get("candidate_name", "")
        amount = session.get("amount", 0)
        return (
            f"{vote_count} vote(s) for {candidate_name}: {symbol}{amount / 100:.2f}.\n"
            "Proceed to checkout?\n1. Proceed\n2. Cancel",
            False,
        )

    async def _handle_checkout_confirm(
        self, phone: str, text: str, session: dict
    ) -> Tuple[str, bool]:
        choice = text.strip()
        if choice == "2":
            await self._clear_session(phone)
            return ("Vote cancelled.", True)
        if choice != "1":
            return ("Reply 1 to proceed or 2 to cancel:", False)

        provider = session.get("provider", "")
        provider_label = session.get("provider_label", "")
        return await self._charge_and_respond(phone, session, provider, provider_label)

    async def _charge_and_respond(
        self, phone: str, session: dict, provider: str, provider_label: str
    ) -> Tuple[str, bool]:
        parent, parent_kind = await self._reload_parent(session)
        if not parent:
            await self._clear_session(phone)
            return ("No longer available. Dial in again to retry.", True)

        category_id = UUID(session["category_id"])
        candidate_id = UUID(session["candidate_id"])
        candidate_name = session.get("candidate_name", "")
        voter_hash = session["voter_hash"]
        vote_count = session.get("vote_count", 1)
        total_amount = session.get("amount", session.get("vote_price", 0))

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
                "amount": total_amount,
                "currency": settings.VOTE_CURRENCY,
                "status": "pending",
            }
        )

        junipay = JunipayService(
            settings.JUNIPAY_CLIENT_ID, settings.JUNIPAY_PRIVATE_KEY
        )
        callback_url = f"{settings.PUBLIC_BASE_URL}/api/v1/payments/junipay/webhook"
        try:
            charge = await junipay.charge_mobile_money(
                email=placeholder_email,
                amount=total_amount,
                reference=reference,
                phone=phone,
                provider=provider,
                callback_url=callback_url,
                description=f"Pollord vote: {candidate_name}",
            )
        except HTTPException as exc:
            logger.error(
                "USSD charge_mobile_money rejected ref=%s provider=%s: %s",
                reference,
                provider,
                exc.detail,
            )
            await self.txn_repo.update_status(reference, "failed")
            await self._clear_session(phone)
            return (f"Payment could not be started: {exc.detail}", True)

        logger.info(
            "USSD charge_mobile_money ref=%s provider=%s status=%s",
            reference,
            provider,
            charge.get("status"),
        )

        # Lets the JuniPay webhook (junipay_webhook.py) know which phone to text.
        await self.redis.setex(
            f"ussd:payment:{reference}",
            PAYMENT_TTL,
            json.dumps({"phone": phone}),
        )

        symbol = _CURRENCY_SYMBOL.get(settings.VOTE_CURRENCY, settings.VOTE_CURRENCY)
        amount_display = f"{symbol}{total_amount / 100:.2f}"
        vote_label = f"{vote_count} vote(s)" if vote_count > 1 else "a vote"
        amount_sentence = f"{amount_display}, {vote_label} for {candidate_name}"

        await self._clear_session(phone)
        # JuniPay's /payment response carries no per-network next-step text
        # (unlike Paystack's display_text), so this instruction is always ours.
        if provider == "mtn":
            instruction = "Approve the MTN prompt on your phone or dial *170#"
        else:
            instruction = f"Check your phone and approve the {provider_label} prompt"
        return (
            _join_sentence(amount_sentence, f"{instruction}. SMS on confirm."),
            True,
        )

    async def _cast_vote_directly(
        self, phone: str, parent, parent_kind: str, category, candidate, voter_hash: str
    ) -> Tuple[str, bool]:
        now = datetime.now(timezone.utc)
        encrypted = self.crypto.encrypt_vote_data([str(candidate.candidate_id)])
        cast_at = now.isoformat()
        signature = self.crypto.sign_vote(encrypted, cast_at)

        try:
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
        except IntegrityError:
            # has_voted() upstream is a check-then-insert race under real
            # concurrent load — uq_vote_per_category is the atomic backstop.
            return (f"You have already voted in {category.name}.", True)
        _ussd_election_id = parent.election_id if parent_kind == "election" else None
        _ussd_event_id = parent.event_id if parent_kind == "event" else None
        await notify_vote_cast(_ussd_election_id, _ussd_event_id)
        await notify_vote_cast_if_due(self.db, _ussd_election_id, _ussd_event_id)

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


def _find_candidate_by_code(parent, code: str):
    """Looks up a nominee by short code across every category, so the voter
    never has to pick a category first. Returns (None, None) if no match."""
    code = code.upper()
    for category in sorted(parent.categories, key=lambda c: c.display_order):
        for candidate in category.candidates:
            if candidate.short_code and candidate.short_code.upper() == code:
                return category, candidate
    return None, None
