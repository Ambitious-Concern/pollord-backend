"""
USSD webhook — Arkesel gateway.

POST /api/v1/ussd/arkesel/callback?token=...

Arkesel's callback has no signature scheme of its own, so `token` (a secret
you generate yourself, e.g. `openssl rand -hex 16`) is required as a query
param on the URL you register with them — anyone hitting this endpoint
without it is rejected before touching the DB or session state.

Contract (confirmed against Arkesel's own USSD API docs):
  Request:  form-encoded POST — sessionId, serviceCode, phoneNumber, text
            `text` is cumulative, `*`-separated (same convention as
            Africa's Talking) — e.g. "2*0241234567" for Main Menu -> option 2
            -> that input. Only the last segment is the latest keypress.
  Response: plain text, prefixed "CON " (show a menu, session stays open) or
            "END " (final message, session closes). Not JSON.
"""
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import get_redis
from app.db.base import get_db
from app.services.ussd_conversation_service import UssdConversationService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ussd", tags=["USSD"])


@router.post("/arkesel/callback", response_class=PlainTextResponse)
async def arkesel_callback(
    token: str = Query(""),
    sessionId: str = Form(""),
    serviceCode: str = Form(""),
    phoneNumber: str = Form(""),
    text: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if not settings.ARKESEL_WEBHOOK_TOKEN or token != settings.ARKESEL_WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

    if not phoneNumber:
        return PlainTextResponse("END Missing phone number.")

    # Per-phone rate limiting — same threshold as the WhatsApp webhook.
    redis = await get_redis()
    rate_key = f"ussd:rate:{phoneNumber}"
    count = await redis.incr(rate_key)
    if count == 1:
        await redis.expire(rate_key, 60)
    if count > 15:
        return PlainTextResponse("END Too many requests. Please try again shortly.")

    # `text` accumulates every input across the session; only the latest
    # keypress is relevant to advance the state machine.
    inputs = text.split("*") if text else []
    latest_input = inputs[-1] if inputs else ""

    try:
        service = UssdConversationService(db=db, redis=redis)
        message, end_session = await service.handle(phone=phoneNumber, text=latest_input)
    except Exception:
        logger.exception("Error handling USSD session %s from %s", sessionId, phoneNumber[:4] + "****")
        message, end_session = "Sorry, something went wrong. Please dial in again.", True

    prefix = "END" if end_session else "CON"
    return PlainTextResponse(f"{prefix} {message}")
