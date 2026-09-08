import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class ArkeselService:
    """Thin client for Arkesel's SMS v2 API — used to confirm a USSD vote's
    outcome after the fact, since a USSD session (~180s) is almost always
    over before a mobile money payment approval and Paystack's webhook land."""

    BASE_URL = "https://sms.arkesel.com/api/v2/sms/send"

    async def send_sms(self, to: str, message: str) -> None:
        payload = {
            "sender": settings.ARKESEL_SMS_SENDER_ID,
            "message": message,
            "recipients": [to],
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                self.BASE_URL,
                json=payload,
                headers={"api-key": settings.ARKESEL_API_KEY},
            )
            if resp.status_code >= 400:
                logger.error(
                    "Arkesel SMS send failed: status=%s body=%s",
                    resp.status_code,
                    resp.text,
                )
            resp.raise_for_status()
