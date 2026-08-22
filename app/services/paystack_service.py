import httpx
from fastapi import HTTPException

# Paystack replying HTTP 200 with `status: false` is a *successful* call that
# carries a business rejection — "You cannot initiate third party payouts as a
# starter business", "Your balance is not enough to fulfil this request". That
# is not a gateway failure, and it must not be reported as 5xx: production sits
# behind Cloudflare, which replaces any origin 5xx body with its own generic
# "Error 502: Bad gateway" page. Doing so hid the only useful part of the
# response, so an admin paying an organizer saw "retry in 60 seconds" and never
# the reason Paystack refused. 4xx passes through the edge intact.
PAYSTACK_REJECTED = 400


class PaystackService:
    BASE_URL = "https://api.paystack.co"

    def __init__(self, secret_key: str):
        self._headers = {
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/json",
        }

    async def initialize_transaction(
        self,
        email: str,
        amount: int,
        reference: str,
        metadata: dict | None = None,
        currency: str = "NGN",
    ) -> dict:
        """
        Initialize a Paystack transaction.
        Returns the Paystack `data` payload which includes
        `authorization_url`, `access_code`, and `reference`.
        """
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.BASE_URL}/transaction/initialize",
                headers=self._headers,
                json={
                    "email": email,
                    "amount": amount,
                    "reference": reference,
                    "currency": currency,
                    "metadata": metadata or {},
                },
                timeout=30.0,
            )

        body = r.json()
        if not body.get("status"):
            raise HTTPException(
                status_code=PAYSTACK_REJECTED,
                detail=f"Paystack error: {body.get('message', 'Unknown error')}",
            )
        return body["data"]

    async def verify_transaction(self, reference: str) -> dict:
        """
        Verify a transaction by reference.
        Returns the Paystack `data` payload.
        Raises HTTPException if verification fails or payment wasn't successful.
        """
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{self.BASE_URL}/transaction/verify/{reference}",
                headers=self._headers,
                timeout=30.0,
            )

        body = r.json()
        if not body.get("status"):
            raise HTTPException(
                status_code=PAYSTACK_REJECTED,
                detail=f"Paystack error: {body.get('message', 'Unknown error')}",
            )
        return body["data"]

    # --- Transfers (paying money out — collecting payments above never touches this) ---

    async def list_banks(self, currency: str = "GHS", transfer_type: str = "mobile_money") -> list[dict]:
        """List Paystack's own bank/mobile-money-provider codes for a currency —
        fetched live rather than hardcoded, since a wrong code here means money
        could be sent to the wrong destination."""
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{self.BASE_URL}/bank",
                headers=self._headers,
                params={"currency": currency, "type": transfer_type},
                timeout=30.0,
            )

        body = r.json()
        if not body.get("status"):
            raise HTTPException(
                status_code=PAYSTACK_REJECTED,
                detail=f"Paystack error: {body.get('message', 'Unknown error')}",
            )
        return body["data"]

    async def resolve_account(self, account_number: str, bank_code: str) -> dict:
        """Confirms an account/mobile-money number resolves to a real account
        before we ever create a transfer recipient for it. Returns
        {account_number, account_name, bank_id}."""
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{self.BASE_URL}/bank/resolve",
                headers=self._headers,
                params={"account_number": account_number, "bank_code": bank_code},
                timeout=30.0,
            )

        body = r.json()
        if not body.get("status"):
            raise HTTPException(
                status_code=PAYSTACK_REJECTED,
                detail=f"Could not verify that account: {body.get('message', 'Unknown error')}",
            )
        return body["data"]

    async def create_transfer_recipient(
        self,
        name: str,
        account_number: str,
        bank_code: str,
        currency: str = "GHS",
        recipient_type: str = "mobile_money",
    ) -> dict:
        """Returns the created recipient, notably `recipient_code`."""
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.BASE_URL}/transferrecipient",
                headers=self._headers,
                json={
                    "type": recipient_type,
                    "name": name,
                    "account_number": account_number,
                    "bank_code": bank_code,
                    "currency": currency,
                },
                timeout=30.0,
            )

        body = r.json()
        if not body.get("status"):
            raise HTTPException(
                status_code=PAYSTACK_REJECTED,
                detail=f"Paystack error creating transfer recipient: {body.get('message', 'Unknown error')}",
            )
        return body["data"]

    async def initiate_transfer(
        self, amount: int, recipient_code: str, reason: str, reference: str
    ) -> dict:
        """Moves real money out of the Paystack balance. `amount` is in the
        smallest currency unit (pesewas for GHS). Returns the transfer object;
        `data["status"]` may be "success", "pending", or "otp" (Paystack's
        account-level settings require a one-time code sent to the business's
        phone to finalize — that can't be completed from this API alone)."""
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.BASE_URL}/transfer",
                headers=self._headers,
                json={
                    "source": "balance",
                    "amount": amount,
                    "recipient": recipient_code,
                    "reason": reason,
                    "reference": reference,
                },
                timeout=30.0,
            )

        body = r.json()
        if not body.get("status"):
            raise HTTPException(
                status_code=PAYSTACK_REJECTED,
                detail=f"Paystack error initiating transfer: {body.get('message', 'Unknown error')}",
            )
        return body["data"]
