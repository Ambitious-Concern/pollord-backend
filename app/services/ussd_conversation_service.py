"""
USSD conversation state machine (Arkesel gateway).

States (stored per phone number in Redis, TTL = USSD_SESSION_TTL_SECONDS —
matches Arkesel's own ~180s session timeout):
  idle              → greet, ask for the election/event's USSD code
  awaiting_category → parent has >1 category with candidates, waiting for a number
  awaiting_vote     → ballot shown, waiting for one candidate short code
  awaiting_vote_count  → paid vote — waiting for how many votes to buy (charge
                         is vote_price * count; free votes skip this entirely)
  awaiting_network     → paid vote — waiting for a mobile money network choice
                         (only reached if the dialing number's prefix isn't a
                         recognized network block — otherwise skipped, see
                         _detect_network)
  awaiting_payment_otp → some providers need Paystack's OTP relayed back via
                         /charge/submit_otp before the debit completes; others
                         ("pay_offline") need nothing further from us at all

Voter identity: phone number is HMAC-hashed per category — never stored in
plaintext, same convention as generate_whatsapp_voter_hash.

Payment: unlike the web/WhatsApp redirect flow, USSD has no browser to send a
link to — a direct Paystack mobile money charge is initiated against the same
phone number that dialed in. If the charge comes back "pay_offline", the
session ends immediately with instructions to approve on-phone. If it comes
back "send_otp" instead, the session stays open one more turn to collect and
relay the OTP. Either way, final completion is always reported later via the
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

# Original Ghana number-block allocations, used to skip the manual network
# menu entirely — voters are dialing from the phone that already carries
# their money, so the number itself tells us the network. This is a
# best-effort guess: a number ported to another network will guess wrong,
# in which case the charge attempt simply fails with a clear error (existing
# behavior) rather than silently mischarging anything.
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
    """Join two sentence fragments without a doubled period — needed because
    `first` is often Paystack's own display_text, which usually already ends
    with its own punctuation."""
    return f"{first.rstrip('. ')}. {second}"


def _detect_network(phone: str) -> Optional[Tuple[str, str]]:
    """Best-guess (provider, label) from a Ghana MSISDN's original block
    allocation, or None if the prefix isn't recognized (caller should fall
    back to asking). Accepts 233XXXXXXXXX, 0XXXXXXXXX, or bare XXXXXXXXX."""
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

        if state == "awaiting_category":
            return await self._handle_category_selection(phone, text, session)
        if state == "awaiting_vote":
            return await self._handle_vote_input(phone, text, session)
        if state == "awaiting_vote_count":
            return await self._handle_vote_count(phone, text, session)
        if state == "awaiting_network":
            return await self._handle_network_selection(phone, text, session)
        if state == "awaiting_payment_otp":
            return await self._handle_payment_otp(phone, text, session)

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

        header = f"{parent.title}: {category.name}"
        price_line = None
        if vote_price > 0:
            symbol = _CURRENCY_SYMBOL.get(
                settings.VOTE_CURRENCY, settings.VOTE_CURRENCY
            )
            price_line = f"Cost: {symbol}{vote_price / 100:.2f}/vote"

        full_lines = [header, "Enter a candidate code:"]
        full_lines += [f"{c.short_code}: {c.name}" for c in candidates if c.short_code]
        if price_line:
            full_lines.append(price_line)
        full_message = "\n".join(full_lines)

        if len(full_message) <= settings.USSD_MAX_MESSAGE_LENGTH:
            message = full_message
        else:
            # Too many nominees to list on one screen — ask for the code
            # directly instead (organizers publish per-nominee codes on
            # posters/social media for exactly this case).
            lines = [header, "Enter your nominee's code:"]
            if price_line:
                lines.append(price_line)
            message = "\n".join(lines)

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
        return (message, False)

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
                "state": "awaiting_vote_count",
                "parent_id": str(_parent_id(parent, parent_kind)),
                "parent_kind": parent_kind,
                "category_id": str(category_id),
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

        # The dialing phone is the same one paying — no need to ask which
        # network it's on when the number itself already tells us.
        detected = _detect_network(phone)
        if detected:
            provider, provider_label = detected
            return await self._charge_and_respond(
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

        paystack = PaystackService(settings.PAYSTACK_SECRET_KEY)
        try:
            charge = await paystack.charge_mobile_money(
                email=placeholder_email,
                amount=total_amount,
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
            logger.error(
                "USSD charge_mobile_money rejected ref=%s provider=%s: %s",
                reference, provider, exc.detail,
            )
            await self.txn_repo.update_status(reference, "failed")
            await self._clear_session(phone)
            return (f"Payment could not be started: {exc.detail}", True)

        logger.info(
            "USSD charge_mobile_money ref=%s provider=%s status=%s display_text=%s",
            reference, provider, charge.get("status"), charge.get("display_text"),
        )

        # So the Paystack webhook (payments.py) knows to text this phone,
        # regardless of which branch below we take.
        await self.redis.setex(
            f"ussd:payment:{reference}",
            PAYMENT_TTL,
            json.dumps({"phone": phone}),
        )

        symbol = _CURRENCY_SYMBOL.get(settings.VOTE_CURRENCY, settings.VOTE_CURRENCY)
        amount_display = f"{symbol}{total_amount / 100:.2f}"
        vote_label = f"{vote_count} vote(s)" if vote_count > 1 else "a vote"

        # Paystack tells us exactly what the customer needs to do next for
        # this specific network/processor (e.g. MTN GH is often "Dial *170#
        # and enter your PIN to complete this transaction" rather than an
        # automatic on-phone push) — relay that verbatim when present instead
        # of guessing our own generic instruction.
        display_text = charge.get("display_text")

        if charge.get("status") == "send_otp":
            # This provider needs the OTP the customer just received relayed
            # back to Paystack before the charge finishes.
            await self._set_session(
                phone,
                {
                    "state": "awaiting_payment_otp",
                    "reference": reference,
                    "candidate_name": candidate_name,
                    "vote_count": vote_count,
                },
            )
            prompt = display_text or "Enter the OTP sent to your phone"
            return (
                _join_sentence(
                    prompt, f"Confirm {amount_display}, {vote_label} for {candidate_name}:"
                ),
                False,
            )

        await self._clear_session(phone)
        instruction = display_text or f"Check your phone and approve the {provider_label} prompt"
        return (
            _join_sentence(
                instruction,
                f"{amount_display}, {vote_label} for {candidate_name}. SMS on confirm.",
            ),
            True,
        )

    async def _handle_payment_otp(
        self, phone: str, text: str, session: dict
    ) -> Tuple[str, bool]:
        otp = "".join(ch for ch in text if ch.isdigit())
        if not otp:
            return ("Enter the numeric OTP sent to your phone:", False)

        reference = session["reference"]
        candidate_name = session.get("candidate_name", "")
        vote_count = session.get("vote_count", 1)
        vote_label = f"{vote_count} vote(s)" if vote_count > 1 else "your vote"
        await self._clear_session(phone)

        paystack = PaystackService(settings.PAYSTACK_SECRET_KEY)
        try:
            result = await paystack.submit_otp(otp=otp, reference=reference)
        except HTTPException as exc:
            logger.error("USSD submit_otp rejected ref=%s: %s", reference, exc.detail)
            await self.txn_repo.update_status(reference, "failed")
            return (f"Payment failed: {exc.detail}. Dial in again to retry.", True)

        logger.info(
            "USSD submit_otp ref=%s status=%s display_text=%s",
            reference, result.get("status"), result.get("display_text"),
        )

        instruction = result.get("display_text") or "Payment submitted"
        return (
            _join_sentence(instruction, f"{vote_label} for {candidate_name}. SMS on confirm."),
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
