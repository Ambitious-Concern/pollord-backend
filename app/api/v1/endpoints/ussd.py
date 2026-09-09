"""
USSD webhook — Arkesel gateway.

POST /api/v1/ussd/arkesel/callback?token=...

Arkesel's callback has no signature scheme of its own, so `token` (a secret
you generate yourself, e.g. `openssl rand -hex 16`) is required as a query
param on the URL you register with them — anyone hitting this endpoint
without it is rejected before touching the DB or session state.

Contract (confirmed against Arkesel's actual developer docs + their own
"Test your USSD service" simulator — NOT the Africa's Talking-style plain
text convention the first version of this file assumed):

  Request (JSON):
    sessionID   — Arkesel's own session id. Not used for our state (see
                  ussd_conversation_service.py, which tracks state in Redis
                  keyed by phone number instead) — only echoed back.
    userID      — id Arkesel assigned this account at subscription time.
                  Echoed back verbatim, never generated or interpreted.
    msisdn      — the subscriber's phone number.
    newSession  — true on the very first request of a dial. On that first
                  request `userData` holds the dialed extension itself
                  (e.g. "*928*928#"), not real input, so it's ignored.
    userData    — the latest keypress only (NOT cumulative/`*`-joined like
                  Africa's Talking's `text` — every request carries just
                  the one new input).

  Response (JSON), all fields required:
    sessionID, userID, msisdn — echoed back exactly as received.
    message         — text to show on the phone.
    continueSession — true to keep the session open (menu), false to end it.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import get_redis
from app.db.base import get_db
from app.services.ussd_conversation_service import UssdConversationService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ussd", tags=["USSD"])


class ArkeselUssdRequest(BaseModel):
    sessionID: str = ""
    userID: str = ""
    msisdn: str = ""
    newSession: bool = False
    userData: str = ""


class ArkeselUssdResponse(BaseModel):
    sessionID: str
    userID: str
    msisdn: str
    message: str
    continueSession: bool


@router.post("/arkesel/callback", response_model=ArkeselUssdResponse)
async def arkesel_callback(
    data: ArkeselUssdRequest,
    token: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    if not settings.ARKESEL_WEBHOOK_TOKEN or token != settings.ARKESEL_WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

    def _reply(message: str, continue_session: bool) -> ArkeselUssdResponse:
        # Last-resort safety net: every individual screen (ballot, network
        # choice, OTP prompt, confirmation...) is expected to already fit,
        # but a message built from Paystack's own free-text display_text
        # can't be size-checked ahead of time - truncate rather than let a
        # too-long message get silently mangled or dropped by a carrier.
        if len(message) > settings.USSD_MAX_MESSAGE_LENGTH:
            logger.warning(
                "USSD message exceeded %d chars (%d), truncating: %r",
                settings.USSD_MAX_MESSAGE_LENGTH, len(message), message,
            )
            message = message[: settings.USSD_MAX_MESSAGE_LENGTH - 1] + "…"
        return ArkeselUssdResponse(
            sessionID=data.sessionID,
            userID=data.userID,
            msisdn=data.msisdn,
            message=message,
            continueSession=continue_session,
        )

    if not data.msisdn:
        return _reply("Missing phone number.", False)

    # Per-phone rate limiting — same threshold as the WhatsApp webhook.
    redis = await get_redis()
    rate_key = f"ussd:rate:{data.msisdn}"
    count = await redis.incr(rate_key)
    if count == 1:
        await redis.expire(rate_key, 60)
    if count > 15:
        return _reply("Too many requests. Please try again shortly.", False)

    # On the first hit of a dial, userData is the extension itself, not
    # user input — treat it as "no input yet" so the state machine shows
    # the welcome/enter-code prompt instead of trying to look up a code.
    latest_input = "" if data.newSession else data.userData

    try:
        service = UssdConversationService(db=db, redis=redis)
        message, end_session = await service.handle(phone=data.msisdn, text=latest_input)
    except Exception:
        logger.exception(
            "Error handling USSD session %s from %s", data.sessionID, data.msisdn[:4] + "****"
        )
        message, end_session = "Sorry, something went wrong. Please dial in again.", True

    return _reply(message, not end_session)
