"""Paystack answering "no" must not be reported as a gateway failure.

When Paystack replies HTTP 200 with `status: false`, the API call succeeded —
it just carried a business rejection ("You cannot initiate third party payouts
as a starter business", "Your balance is not enough to fulfil this request").

These used to raise 502. In production the origin sits behind Cloudflare,
which replaces any origin 5xx body with its own generic "Error 502: Bad
gateway" page — so the admin trying to pay an organizer saw only "retry in
60 seconds" and never Paystack's actual reason. The status must be 4xx for
the message to survive the edge.
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from app.services.paystack_service import PaystackService


class FakeResponse:
    """Paystack's rejection shape: a successful HTTP call, status false."""

    status_code = 200

    def __init__(self, message: str):
        self._message = message

    def json(self):
        return {"status": False, "message": self._message}


def _service() -> PaystackService:
    return PaystackService("sk_test_notreal")


async def _capture(coro) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        await coro
    return exc.value


@pytest.mark.asyncio
class TestPaystackRejectionStatusCodes:
    async def test_initiate_transfer_rejection_is_4xx(self):
        message = "You cannot initiate third party payouts as a starter business"
        with patch.object(
            httpx.AsyncClient, "post", new=AsyncMock(return_value=FakeResponse(message))
        ):
            err = await _capture(
                _service().initiate_transfer(
                    amount=50000,
                    recipient_code="RCP_x",
                    reason="Pollord payout",
                    reference="payout_abc",
                )
            )

        assert 400 <= err.status_code < 500, (
            f"got {err.status_code}; a 5xx is swallowed by Cloudflare and the "
            "admin never learns why the payout failed"
        )
        assert message in err.detail

    async def test_create_transfer_recipient_rejection_is_4xx(self):
        message = "Invalid bank code"
        with patch.object(
            httpx.AsyncClient, "post", new=AsyncMock(return_value=FakeResponse(message))
        ):
            err = await _capture(
                _service().create_transfer_recipient(
                    name="Jane",
                    account_number="0244000000",
                    bank_code="MTN",
                )
            )

        assert 400 <= err.status_code < 500
        assert message in err.detail

    async def test_verify_transaction_rejection_is_4xx(self):
        """Also reached by the Add Attendee form, which reports Paystack's
        message inline next to the reference field."""
        message = "Transaction reference not found"
        with patch.object(
            httpx.AsyncClient, "get", new=AsyncMock(return_value=FakeResponse(message))
        ):
            err = await _capture(_service().verify_transaction("bogus_ref"))

        assert 400 <= err.status_code < 500
        assert message in err.detail

    async def test_initialize_transaction_rejection_is_4xx(self):
        message = "Invalid amount"
        with patch.object(
            httpx.AsyncClient, "post", new=AsyncMock(return_value=FakeResponse(message))
        ):
            err = await _capture(
                _service().initialize_transaction(
                    email="a@example.com",
                    amount=100,
                    reference="ref_x",
                )
            )

        assert 400 <= err.status_code < 500
        assert message in err.detail

    async def test_list_banks_rejection_is_4xx(self):
        message = "Unsupported currency"
        with patch.object(
            httpx.AsyncClient, "get", new=AsyncMock(return_value=FakeResponse(message))
        ):
            err = await _capture(_service().list_banks(currency="XXX"))

        assert 400 <= err.status_code < 500
        assert message in err.detail

    async def test_resolve_account_already_used_4xx(self):
        """This one was already correct — pinning it so the others converge
        on its convention rather than drifting back."""
        message = "Could not resolve account name"
        with patch.object(
            httpx.AsyncClient, "get", new=AsyncMock(return_value=FakeResponse(message))
        ):
            err = await _capture(_service().resolve_account("0244000000", "MTN"))

        assert 400 <= err.status_code < 500
        assert message in err.detail
