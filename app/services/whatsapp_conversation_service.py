"""
WhatsApp conversation state machine.

States (stored per phone number in Redis, TTL = 10 min):
  idle              → greet, ask for election ID
  awaiting_vote     → ballot shown, waiting for candidate short code(s)
  awaiting_payment  → payment link sent, waiting for Paystack webhook

Voter identity: phone number is HMAC-hashed per election — never stored in plaintext.
Payment: Paystack redirect flow. Vote is cast automatically when charge.success fires.
"""
import hashlib
import json
import logging
import re
import secrets as _secrets
import uuid as _uuid
from datetime import datetime, timezone
from uuid import UUID

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import generate_whatsapp_voter_hash
from app.models.audit_log import AuditLog
from app.models.election import Election
from app.models.platform_setting import PlatformSetting
from app.models.transaction import Transaction
from app.models.vote import Vote
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.election_repository import ElectionRepository
from app.repositories.transaction_repository import TransactionRepository
from app.repositories.vote_repository import VoteRepository
from app.services.cryptography_service import CryptographyService
from app.services.paystack_service import PaystackService

logger = logging.getLogger(__name__)

SESSION_TTL = 600       # 10 min inactivity → reset session
PAYMENT_TTL = 3600      # 1 hr for pending payment data in Redis

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

_CURRENCY_SYMBOL = {"GHS": "₵", "NGN": "₦", "USD": "$", "EUR": "€"}

HELP_TEXT = (
    "Welcome to *Pollord Voting* via WhatsApp!\n\n"
    "To vote, send:\n"
    "  *VOTE <election-id>*\n\n"
    "To cancel at any time, send *CANCEL*.\n"
    "To check results, send *RESULTS <election-id>*."
)


class WhatsAppConversationService:
    def __init__(self, db: AsyncSession, redis: aioredis.Redis):
        self.db = db
        self.redis = redis
        self.crypto = CryptographyService()
        self.election_repo = ElectionRepository(Election, db)
        self.vote_repo = VoteRepository(Vote, db)
        self.txn_repo = TransactionRepository(db)
        self.audit_repo = AuditLogRepository(AuditLog, db)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def handle(self, phone: str, text: str) -> str:
        """Process an incoming message and return the reply text."""
        text = text.strip()
        session = await self._get_session(phone)
        state = session.get("state", "idle")
        normalized = text.upper()

        if normalized in ("CANCEL", "QUIT", "EXIT", "STOP"):
            await self._clear_session(phone)
            return "Voting session cancelled. Send *VOTE <election-id>* any time to start again."

        if normalized in ("HELP", "HI", "HELLO", "START", "MENU"):
            await self._clear_session(phone)
            return HELP_TEXT

        if normalized.startswith("RESULTS"):
            return await self._handle_results(normalized)

        if state == "awaiting_payment":
            reference = session.get("reference", "")
            symbol = _CURRENCY_SYMBOL.get(settings.VOTE_CURRENCY, settings.VOTE_CURRENCY)
            return (
                f"Your payment is still pending.\n\n"
                f"Complete payment via the link sent earlier, or send *CANCEL* to start over.\n\n"
                f"Reference: `{reference}`"
            )

        if state == "awaiting_vote":
            return await self._handle_vote_input(phone, text, session)

        return await self._handle_election_lookup(phone, text)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_election_lookup(self, phone: str, text: str) -> str:
        match = _UUID_RE.search(text)
        if not match:
            stripped = re.sub(r"^VOTE\s+", "", text, flags=re.IGNORECASE).strip()
            match = _UUID_RE.search(stripped)

        if not match:
            return (
                "Please send your *Election ID* to start voting.\n"
                "Example: *VOTE abc12345-...*\n\n"
                "Send *HELP* for more options."
            )

        try:
            election_id = UUID(match.group())
        except ValueError:
            return "That doesn't look like a valid Election ID. Please check and try again."

        election = await self.election_repo.get_with_candidates(election_id)
        if not election:
            return "Election not found. Please check the ID and try again."

        if not (election.visibility == "public" and not election.require_verification):
            return (
                "This election requires account verification.\n"
                f"Please vote at: {_frontend_vote_url(election_id)}"
            )

        if election.status != "active":
            status_msg = {
                "draft": "This election has not started yet.",
                "scheduled": "This election has not started yet.",
                "completed": "This election has ended.",
            }.get(election.status, f"This election is currently *{election.status}*.")
            return status_msg

        now = datetime.now(timezone.utc)
        if now < election.start_datetime or now > election.end_datetime:
            return "This election is outside its voting window."

        allow_revoting = getattr(election, "allow_revoting", False)
        if not allow_revoting:
            voter_hash = generate_whatsapp_voter_hash(phone, election_id)
            if await self.vote_repo.has_voted(voter_hash, election_id):
                return f"You have already voted in *{election.title}*."

        candidates = sorted(election.candidates, key=lambda c: c.display_order)
        if not candidates:
            return "This election has no candidates yet."

        election_type = election.election_type
        max_sel = getattr(election, "max_selections", None)
        vote_price = await self._get_vote_price(election)

        lines = [f"🗳️ *{election.title}*"]
        if election.description:
            lines.append(election.description)
        lines.append("")

        if election_type == "single_choice":
            lines.append("Reply with *one candidate code* to cast your vote:\n")
        elif election_type == "multiple_choice":
            if max_sel:
                lines.append(f"Reply with up to *{max_sel} candidate codes* (comma-separated):\n")
            else:
                lines.append("Reply with *candidate codes* separated by commas:\n")
        else:
            lines.append("Reply with your candidate code(s):\n")

        for c in candidates:
            lines.append(f"  *{c.short_code}* — {c.name}")

        if vote_price > 0:
            symbol = _CURRENCY_SYMBOL.get(settings.VOTE_CURRENCY, settings.VOTE_CURRENCY)
            lines.append(f"\n💳 Cost: *{symbol}{vote_price / 100:.2f}* per vote")

        lines.append("\nSend *CANCEL* to exit.")

        await self._set_session(phone, {
            "state": "awaiting_vote",
            "election_id": str(election_id),
            "election_title": election.title,
            "election_type": election_type,
            "max_selections": max_sel,
            "allow_revoting": allow_revoting,
            "vote_price": vote_price,
        })

        return "\n".join(lines)

    async def _handle_vote_input(self, phone: str, text: str, session: dict) -> str:
        election_id = UUID(session["election_id"])
        election_type = session.get("election_type", "single_choice")
        max_sel = session.get("max_selections")
        allow_revoting = session.get("allow_revoting", False)
        vote_price = session.get("vote_price", 0)

        election = await self.election_repo.get_with_candidates(election_id)
        if not election:
            await self._clear_session(phone)
            return "Election no longer available. Send *VOTE <election-id>* to try again."

        if election.status != "active":
            await self._clear_session(phone)
            return "This election is no longer active."

        now = datetime.now(timezone.utc)
        if now < election.start_datetime or now > election.end_datetime:
            await self._clear_session(phone)
            return "This election is outside its voting window."

        # Parse short codes
        raw_codes = [c.strip().upper() for c in re.split(r"[,\s]+", text) if c.strip()]
        if not raw_codes:
            return "Please reply with a candidate code. Send *CANCEL* to exit."

        code_map = {
            c.short_code.upper(): c.candidate_id
            for c in election.candidates
            if c.short_code
        }

        candidate_ids = []
        unknown = []
        for code in raw_codes:
            cid = code_map.get(code)
            if cid:
                candidate_ids.append(cid)
            else:
                unknown.append(code)

        if unknown:
            valid_list = "  " + "\n  ".join(f"*{k}*" for k in sorted(code_map))
            return (
                f"Unknown code(s): *{', '.join(unknown)}*\n\n"
                f"Valid codes are:\n{valid_list}\n\n"
                "Send *CANCEL* to exit."
            )

        if election_type == "single_choice" and len(candidate_ids) != 1:
            return "This election requires *exactly one* candidate code."

        if max_sel and len(candidate_ids) > max_sel:
            return f"You can select at most *{max_sel}* candidate(s)."

        # Deduplicate
        seen: set = set()
        candidate_ids = [cid for cid in candidate_ids if not (cid in seen or seen.add(cid))]

        # Voter hash (computed now so it's consistent between initiation and webhook)
        base_hash = generate_whatsapp_voter_hash(phone, election_id)
        if allow_revoting:
            voter_hash = hashlib.sha256(f"{base_hash}:{_uuid.uuid4().hex}".encode()).hexdigest()
        else:
            voter_hash = base_hash
            if await self.vote_repo.has_voted(voter_hash, election_id):
                await self._clear_session(phone)
                return f"You have already voted in *{election.title}*."

        # Free election — cast vote immediately
        if vote_price == 0:
            return await self._cast_vote_directly(
                phone, election, election_id, candidate_ids, voter_hash, now
            )

        # Paid election — initiate Paystack payment
        return await self._initiate_payment(
            phone, election, election_id, candidate_ids, voter_hash, vote_price
        )

    async def _cast_vote_directly(
        self,
        phone: str,
        election,
        election_id: UUID,
        candidate_ids: list,
        voter_hash: str,
        now: datetime,
    ) -> str:
        encrypted = self.crypto.encrypt_vote_data([str(cid) for cid in candidate_ids])
        cast_at = now.isoformat()
        signature = self.crypto.sign_vote(encrypted, cast_at)

        await self.vote_repo.create({
            "election_id": election_id,
            "voter_hash": voter_hash,
            "vote_data": encrypted,
            "vote_signature": signature,
            "count": 1,
        })

        receipt_code = self.crypto.generate_receipt_code()

        await self.audit_repo.log_action(
            action_type="VOTE_CAST",
            entity_type="Election",
            entity_id=election_id,
            user_id=None,
            ip_address="whatsapp",
            user_agent=f"WhatsApp:{phone[:4]}****",
        )

        await self._clear_session(phone)

        selected_names = [
            next(c.name for c in election.candidates if c.candidate_id == cid)
            for cid in candidate_ids
        ]
        names_str = ", ".join(f"*{n}*" for n in selected_names)

        return (
            f"✅ Vote cast successfully!\n\n"
            f"Election: *{election.title}*\n"
            f"Candidate(s): {names_str}\n\n"
            f"Your receipt code:\n*{receipt_code}*\n\n"
            "Thank you for participating! 🎉"
        )

    async def _initiate_payment(
        self,
        phone: str,
        election,
        election_id: UUID,
        candidate_ids: list,
        voter_hash: str,
        vote_price: int,
    ) -> str:
        reference = f"vote_wa_{_secrets.token_urlsafe(16)}"

        # Use a deterministic placeholder email (no PII stored)
        phone_hash = hashlib.sha256(phone.encode()).hexdigest()[:12]
        placeholder_email = f"wa{phone_hash}@pollord.vote"

        # Persist pending transaction
        await self.txn_repo.create({
            "reference": reference,
            "election_id": election_id,
            "voter_hash": voter_hash,
            "email": placeholder_email,
            "candidate_ids": [str(cid) for cid in candidate_ids],
            "amount": vote_price,
            "currency": settings.VOTE_CURRENCY,
            "status": "pending",
        })

        # Initialize Paystack — returns authorization_url for redirect flow
        paystack = PaystackService(settings.PAYSTACK_SECRET_KEY)
        ps_data = await paystack.initialize_transaction(
            email=placeholder_email,
            amount=vote_price,
            reference=reference,
            currency=settings.VOTE_CURRENCY,
            metadata={
                "election_id": str(election_id),
                "election_title": election.title,
                "channel": "whatsapp",
            },
        )

        # Store phone in Redis so the webhook knows where to send confirmation
        await self.redis.setex(
            f"whatsapp:payment:{reference}",
            PAYMENT_TTL,
            json.dumps({"phone": phone}),
        )

        # Move session to awaiting_payment to block duplicate attempts
        await self._set_session(phone, {
            "state": "awaiting_payment",
            "election_id": str(election_id),
            "reference": reference,
        })

        symbol = _CURRENCY_SYMBOL.get(settings.VOTE_CURRENCY, settings.VOTE_CURRENCY)
        amount_display = f"{symbol}{vote_price / 100:.2f}"

        selected_names = [
            next(c.name for c in election.candidates if c.candidate_id == cid)
            for cid in candidate_ids
        ]
        names_str = ", ".join(f"*{n}*" for n in selected_names)

        return (
            f"💳 *Payment required*\n\n"
            f"Election: *{election.title}*\n"
            f"Candidate(s): {names_str}\n"
            f"Amount: *{amount_display}*\n\n"
            f"Tap to pay:\n{ps_data['authorization_url']}\n\n"
            "Your vote will be cast automatically after payment is confirmed.\n"
            "Send *CANCEL* to cancel."
        )

    async def _handle_results(self, text: str) -> str:
        match = _UUID_RE.search(text)
        if not match:
            return "Please include the Election ID. Example: *RESULTS <election-id>*"

        try:
            election_id = UUID(match.group())
        except ValueError:
            return "Invalid Election ID format."

        election = await self.election_repo.get_with_candidates(election_id)
        if not election:
            return "Election not found."

        allow_result_viewing = getattr(election, "allow_result_viewing", "after_end")
        if allow_result_viewing == "admin_only":
            return "Results for this election are not publicly available."
        if allow_result_viewing == "after_end" and election.status != "completed":
            return "Results will be available after the election ends."

        votes = await self.vote_repo.get_votes_by_election(election_id)
        counts: dict = {}
        total = 0
        for vote in votes:
            weight = getattr(vote, "count", 1)
            total += weight
            decrypted = self.crypto.decrypt_vote_data(vote.vote_data)
            for cid_str in decrypted.get("candidate_ids", []):
                counts[cid_str] = counts.get(cid_str, 0) + weight

        if total == 0:
            return f"No votes have been cast in *{election.title}* yet."

        candidates = sorted(election.candidates, key=lambda c: c.display_order)
        lines = [f"📊 *Results — {election.title}*\n"]
        for c in sorted(candidates, key=lambda c: counts.get(str(c.candidate_id), 0), reverse=True):
            count = counts.get(str(c.candidate_id), 0)
            pct = round(count / total * 100, 1)
            bar = "█" * int(pct / 5)
            lines.append(f"{c.name}: *{count}* votes ({pct}%)\n{bar}")

        lines.append(f"\nTotal votes: *{total}*")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_vote_price(self, election) -> int:
        if getattr(election, "vote_price", None) is not None:
            return election.vote_price
        result = await self.db.execute(
            select(PlatformSetting).where(PlatformSetting.key == "vote_price")
        )
        row = result.scalar_one_or_none()
        if row:
            try:
                return int(row.value)
            except (ValueError, TypeError):
                pass
        return settings.VOTE_PRICE

    def _key(self, phone: str) -> str:
        return f"whatsapp:session:{phone}"

    async def _get_session(self, phone: str) -> dict:
        raw = await self.redis.get(self._key(phone))
        if raw:
            return json.loads(raw)
        return {"state": "idle"}

    async def _set_session(self, phone: str, data: dict) -> None:
        await self.redis.setex(self._key(phone), SESSION_TTL, json.dumps(data))

    async def _clear_session(self, phone: str) -> None:
        await self.redis.delete(self._key(phone))


def _frontend_vote_url(election_id: UUID) -> str:
    return f"{settings.FRONTEND_URL}/voting/ballot/{election_id}"
