import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class WhatsAppService:
    """Thin client for the Meta Cloud API messages endpoint."""

    BASE_URL = "https://graph.facebook.com"

    async def send_text(self, to: str, body: str) -> None:
        url = (
            f"{self.BASE_URL}/{settings.WHATSAPP_API_VERSION}"
            f"/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
        )
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body, "preview_url": False},
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"},
            )
            if resp.status_code >= 400:
                logger.error(
                    "WhatsApp send failed: status=%s body=%s",
                    resp.status_code,
                    resp.text,
                )
            resp.raise_for_status()
