"""
WhatsApp webhook — Meta Cloud API.

GET  /api/v1/whatsapp/webhook  — verification challenge (one-time setup)
POST /api/v1/whatsapp/webhook  — incoming messages
"""
import hashlib
import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import get_redis
from app.db.base import get_db
from app.services.whatsapp_conversation_service import WhatsAppConversationService
from app.services.whatsapp_service import WhatsAppService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/whatsapp", tags=["WhatsApp"])

_wa = WhatsAppService()


# ---------------------------------------------------------------------------
# Webhook verification (GET) — called once by Meta during setup
# ---------------------------------------------------------------------------

@router.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(alias="hub.mode", default=""),
    hub_verify_token: str = Query(alias="hub.verify_token", default=""),
    hub_challenge: str = Query(alias="hub.challenge", default=""),
):
    if hub_mode == "subscribe" and hub_verify_token == settings.WHATSAPP_VERIFY_TOKEN:
        return int(hub_challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


# ---------------------------------------------------------------------------
# Incoming messages (POST)
# ---------------------------------------------------------------------------

@router.post("/webhook", status_code=200)
async def receive_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    body = await request.body()

    # Verify Meta webhook signature when app secret is configured
    if settings.WHATSAPP_APP_SECRET:
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(
            settings.WHATSAPP_APP_SECRET.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            raise HTTPException(status_code=403, detail="Invalid signature")

    payload = await request.json()

    # Meta sends status updates (delivered, read) alongside message events —
    # return 200 immediately so Meta doesn't retry.
    if payload.get("object") != "whatsapp_business_account":
        return {"status": "ignored"}

    redis = await get_redis()
    conversation_svc = WhatsAppConversationService(db=db, redis=redis)

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])

            for msg in messages:
                phone = msg.get("from", "")
                if not phone:
                    continue

                # Per-phone rate limiting: max 15 messages per 60 seconds
                rate_key = f"whatsapp:rate:{phone}"
                count = await redis.incr(rate_key)
                if count == 1:
                    await redis.expire(rate_key, 60)
                if count > 15:
                    continue

                if msg.get("type") != "text":
                    # Non-text message (image, audio, document, etc.)
                    # If the user has an active session, tell them to reply with text.
                    session = await conversation_svc._get_session(phone)
                    if session.get("state", "idle") != "idle":
                        try:
                            await _wa.send_text(
                                to=phone,
                                body="Please reply with *text only*.\nSend *CANCEL* to exit or *HELP* for options.",
                            )
                        except Exception:
                            pass
                    continue

                text = msg.get("text", {}).get("body", "").strip()
                if not text:
                    continue

                try:
                    reply = await conversation_svc.handle(phone=phone, text=text)
                    await _wa.send_text(to=phone, body=reply)
                except Exception:
                    logger.exception("Error handling WhatsApp message from %s", phone[:4] + "****")
                    try:
                        await _wa.send_text(
                            to=phone,
                            body="Sorry, something went wrong. Please try again or send *HELP*.",
                        )
                    except Exception:
                        pass

    return {"status": "ok"}
