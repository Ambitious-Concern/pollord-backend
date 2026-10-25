import time

import httpx
from fastapi import HTTPException
from jose import jwt

JUNIPAY_REJECTED = 400

# Our own network identifiers (shared with Paystack's mobile_money contract)
# mapped to JuniPay's own provider strings.
_PROVIDER_MAP = {"mtn": "mtn", "vod": "vodafone", "atl": "airteltigo"}


class JunipayService:
    """Thin client for JuniPay's Collections API — USSD mobile money charges
    only. Everything else (card/web/WhatsApp payments, payouts) still goes
    through PaystackService."""

    BASE_URL = "https://api.junipayments.com"

    def __init__(self, client_id: str, private_key: str):
        self._client_id = client_id
        # .env stores the PEM on one line with literal \n for newlines.
        self._private_key = private_key.replace("\\n", "\n")

    def _headers(self) -> dict:
        token = jwt.encode(
            {"clientId": self._client_id, "exp": int(time.time()) + 60},
            self._private_key,
            algorithm="RS256",
        )
        return {
            "Authorization": f"Bearer {token}",
            "clientid": self._client_id,
            "Content-Type": "application/json",
        }

    def _raise_for_error(self, r: httpx.Response, body: dict) -> None:
        if r.status_code >= 400 or body.get("status") == "failed":
            raise HTTPException(
                status_code=JUNIPAY_REJECTED,
                detail=f"JuniPay error: {body.get('message', 'Unknown error')}",
            )

    async def charge_mobile_money(
        self,
        email: str,
        amount: int,
        reference: str,
        phone: str,
        provider: str,
        callback_url: str,
        description: str = "Pollord vote",
    ) -> dict:
        """`amount` is pesewas, matching PaystackService's contract — JuniPay
        wants a decimal GHS amount instead. No OTP-relay step exists on this
        endpoint; completion is always reported later via callback_url or
        check_transaction_status."""
        decimal_amount = round(amount / 100, 2)
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.BASE_URL}/payment",
                headers=self._headers(),
                json={
                    "channel": "mobile_money",
                    "phoneNumber": phone,
                    "provider": _PROVIDER_MAP.get(provider, provider),
                    "amount": decimal_amount,
                    "tot_amnt": decimal_amount,
                    "senderEmail": email,
                    "description": description,
                    "foreignID": reference,
                    "callbackUrl": callback_url,
                },
                timeout=30.0,
            )

        body = r.json()
        self._raise_for_error(r, body)
        return body

    async def check_transaction_status(self, trans_id: str) -> dict:
        """Server-to-server confirmation of a charge's real status — used by
        the webhook handler, since JuniPay's callback has no signature to
        verify a POST against forgery. Returns the nested `info` object."""
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.BASE_URL}/checktranstatus",
                headers=self._headers(),
                json={"transID": trans_id},
                timeout=30.0,
            )

        body = r.json()
        if r.status_code >= 400:
            raise HTTPException(
                status_code=JUNIPAY_REJECTED,
                detail=f"JuniPay error: {body.get('message', 'Unknown error')}",
            )
        return body.get("info", body)
