import httpx
from fastapi import HTTPException


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
                status_code=502,
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
                status_code=502,
                detail=f"Paystack error: {body.get('message', 'Unknown error')}",
            )
        return body["data"]
