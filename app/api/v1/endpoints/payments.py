"""
Paystack webhook handler.

POST /api/v1/payments/webhook

Handles charge.success events:
  - Casts the vote tied to the transaction
  - For WhatsApp transactions, sends a confirmation message via WhatsApp
"""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import get_redis
from app.db.base import get_db
from app.models.election import Election
from app.models.event import Event
from app.models.platform_setting import PlatformSetting
from app.models.transaction import Transaction
from app.models.vote import Vote
from app.api.v1.endpoints.tickets import _get_ticketing_service
from app.repositories.election_repository import ElectionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.ticket_transaction_repository import TicketTransactionRepository
from app.repositories.transaction_repository import TransactionRepository
from app.repositories.vote_repository import VoteRepository
from app.services.cryptography_service import CryptographyService
from app.services.whatsapp_service import WhatsAppService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/payments", tags=["Payments"])

_wa = WhatsAppService()
_crypto = CryptographyService()


async def _resolve_vote_price(parent, db: AsyncSession) -> int:
    if getattr(parent, "vote_price", None) is not None:
        return parent.vote_price
    result = await db.execute(
        select(PlatformSetting).where(PlatformSetting.key == "vote_price")
    )
    row = result.scalar_one_or_none()
    if row:
        try:
            return int(row.value)
        except (ValueError, TypeError):
            pass
    return settings.VOTE_PRICE


@router.post("/webhook", status_code=200)
async def paystack_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    body = await request.body()

    # Verify Paystack signature (HMAC-SHA512)
    sig = request.headers.get("x-paystack-signature", "")
    expected = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode(),
        body,
        hashlib.sha512,
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = json.loads(body)
    event = payload.get("event")
    data = payload.get("data", {})

    reference = data.get("reference", "")
    if not reference:
        return {"status": "ignored"}

    if reference.startswith("ticket_"):
        if event == "charge.success":
            return await _handle_ticket_charge_success(reference, data, db)
        if event in ("charge.failed", "charge.abandoned"):
            return await _handle_ticket_charge_failed(reference, data, db)
        return {"status": "ignored"}

    # Route to the correct handler
    if event == "charge.success":
        return await _handle_charge_success(reference, data, db)
    if event in ("charge.failed", "charge.abandoned"):
        return await _handle_charge_failed(reference, data, db)

    return {"status": "ignored"}


async def _notify_whatsapp(reference: str, body: str) -> None:
    """Send a WhatsApp message to the phone linked to this payment reference, then clean up."""
    if not settings.WHATSAPP_ACCESS_TOKEN:
        return
    redis = await get_redis()
    wa_raw = await redis.get(f"whatsapp:payment:{reference}")
    if not wa_raw:
        return
    try:
        wa_data = json.loads(wa_raw)
        phone = wa_data.get("phone", "")
        if phone:
            await _wa.send_text(to=phone, body=body)
            await redis.delete(f"whatsapp:session:{phone}")
    except Exception:
        logger.exception("Failed to send WhatsApp message for ref=%s", reference)
    finally:
        await redis.delete(f"whatsapp:payment:{reference}")


async def _handle_charge_success(reference: str, data: dict, db: AsyncSession) -> dict:
    txn_repo = TransactionRepository(db)
    txn = await txn_repo.get_by_reference(reference)

    if not txn:
        logger.warning("Paystack webhook: unknown reference %s", reference)
        return {"status": "ignored"}

    if txn.status == "success":
        return {"status": "already_processed"}

    # Verify the amount paid is at least what we charged
    paid_amount = data.get("amount", 0)
    if paid_amount < txn.amount:
        await txn_repo.update_status(reference, "failed", data)
        logger.warning(
            "Paystack webhook: amount mismatch ref=%s expected=%s paid=%s",
            reference, txn.amount, paid_amount,
        )
        await _notify_whatsapp(
            reference,
            "❌ *Payment amount mismatch.*\n\nYour vote was not cast. Please contact support.",
        )
        return {"status": "amount_mismatch"}

    if txn.election_id is not None:
        parent = await ElectionRepository(Election, db).get_by_id(
            txn.election_id, id_field="election_id"
        )
        parent_kind = "election"
    else:
        parent = await EventRepository(Event, db).get_by_id(
            txn.event_id, id_field="event_id"
        )
        parent_kind = "event"
    parent_title = parent.title if parent else f"the {parent_kind}"

    # Reject if the election/event closed between payment initiation and this webhook.
    # Elections gate on status + voting window; events are always open while published.
    if parent:
        now = datetime.now(timezone.utc)
        if parent_kind == "election":
            closed = (
                parent.status != "active"
                or now < parent.start_datetime
                or now > parent.end_datetime
            )
        else:
            closed = parent.status != "published"
        if closed:
            await txn_repo.update_status(reference, "failed", data)
            logger.warning(
                "Paystack webhook: %s no longer active ref=%s id=%s status=%s",
                parent_kind, reference, txn.election_id or txn.event_id, parent.status,
            )
            await _notify_whatsapp(
                reference,
                f"⚠️ *Payment received but the {parent_kind} has ended.*\n\n"
                f"{parent_kind.title()}: *{parent_title}*\n\n"
                "Your vote could not be cast. Please contact support to request a refund.",
            )
            return {"status": f"{parent_kind}_ended"}

    # Resolve vote count from the payment amount
    vote_price = await _resolve_vote_price(parent, db) if parent else settings.VOTE_PRICE
    vote_count = max(1, txn.amount // vote_price) if vote_price > 0 else 1

    # Cast the vote
    vote_repo = VoteRepository(Vote, db)
    now = datetime.now(timezone.utc)
    encrypted = _crypto.encrypt_vote_data(txn.candidate_ids)
    cast_at = now.isoformat()
    signature = _crypto.sign_vote(encrypted, cast_at)

    try:
        await vote_repo.create({
            "category_id": txn.category_id,
            "election_id": txn.election_id,
            "event_id": txn.event_id,
            "voter_hash": txn.voter_hash,
            "vote_data": encrypted,
            "vote_signature": signature,
            "count": vote_count,
        })
    except Exception:
        logger.warning("Vote already exists for reference %s, skipping insert", reference)

    await txn_repo.update_status(reference, "success", data)

    receipt_code = _crypto.generate_receipt_code()
    await _notify_whatsapp(
        reference,
        f"✅ Payment confirmed! Vote cast successfully.\n\n"
        f"{parent_kind.title()}: *{parent_title}*\n\n"
        f"Your receipt code:\n*{receipt_code}*\n\n"
        "Thank you for participating! 🎉",
    )
    return {"status": "ok"}


async def _handle_charge_failed(reference: str, data: dict, db: AsyncSession) -> dict:
    txn_repo = TransactionRepository(db)
    txn = await txn_repo.get_by_reference(reference)

    if not txn:
        return {"status": "ignored"}

    if txn.status not in ("pending",):
        return {"status": "ignored"}

    await txn_repo.update_status(reference, "failed", data)
    await _notify_whatsapp(
        reference,
        "❌ *Payment unsuccessful.*\n\n"
        "Your vote was not cast. Send *VOTE <election-id>* to try again.",
    )
    logger.info("Paystack charge failed/abandoned for ref=%s", reference)
    return {"status": "ok"}


async def _handle_ticket_charge_success(reference: str, data: dict, db: AsyncSession) -> dict:
    txn_repo = TicketTransactionRepository(db)
    txn = await txn_repo.get_by_reference(reference)
    if not txn:
        logger.warning("Paystack webhook: unknown ticket reference %s", reference)
        return {"status": "ignored"}
    if txn.status == "success":
        return {"status": "already_processed"}

    service = _get_ticketing_service(db)
    try:
        await service.fulfill_paid_purchase(reference, data, txn_repo)
    except HTTPException as exc:
        logger.warning("Ticket webhook fulfillment failed for ref=%s: %s", reference, exc.detail)
        return {"status": "fulfillment_failed", "detail": exc.detail}
    return {"status": "ok"}


async def _handle_ticket_charge_failed(reference: str, data: dict, db: AsyncSession) -> dict:
    txn_repo = TicketTransactionRepository(db)
    txn = await txn_repo.get_by_reference(reference)
    if not txn:
        return {"status": "ignored"}
    if txn.status != "pending":
        return {"status": "ignored"}
    await txn_repo.update_status(reference, "failed", data)
    logger.info("Paystack ticket charge failed/abandoned for ref=%s", reference)
    return {"status": "ok"}
