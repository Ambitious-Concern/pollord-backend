"""JuniPay webhook handler (USSD mobile money votes only).

POST /api/v1/payments/junipay/webhook

JuniPay's callback carries no signature, so the POSTed status is never
trusted directly — this re-confirms via /checktranstatus (a call scoped to
our own client ID) before dispatching to the same gateway-agnostic
_handle_charge_success/_handle_charge_failed used by the Paystack webhook.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.endpoints.payments import _handle_charge_failed, _handle_charge_success
from app.core.config import settings
from app.db.base import get_db
from app.repositories.transaction_repository import TransactionRepository
from app.services.junipay_service import JunipayService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/payments/junipay", tags=["Payments"])


@router.post("/webhook", status_code=200)
async def junipay_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.json()
    reference = payload.get("foreignID", "")
    trans_id = payload.get("trans_id", "")
    if not reference or not trans_id:
        return {"status": "ignored"}

    junipay = JunipayService(settings.JUNIPAY_CLIENT_ID, settings.JUNIPAY_PRIVATE_KEY)
    try:
        info = await junipay.check_transaction_status(trans_id)
    except Exception:
        logger.exception(
            "JuniPay webhook: status check failed ref=%s trans_id=%s", reference, trans_id
        )
        # Non-2xx (network error or JuniPay 4xx/5xx alike) so JuniPay retries
        # the callback later — our confirmation attempt failed, not
        # necessarily the underlying transaction.
        raise HTTPException(status_code=502, detail="Could not confirm transaction status")

    confirmed_status = str(info.get("status", "")).lower()

    if confirmed_status == "success":
        txn = await TransactionRepository(db).get_by_reference(reference)
        if not txn:
            return {"status": "ignored"}
        # amount comes from our own stored charge, not the webhook body —
        # /checktranstatus doesn't echo it back for independent verification.
        return await _handle_charge_success(reference, {"amount": txn.amount}, db)

    if confirmed_status == "failed":
        return await _handle_charge_failed(reference, payload, db)

    return {"status": "ignored"}
