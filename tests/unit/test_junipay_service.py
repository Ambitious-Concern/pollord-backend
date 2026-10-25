"""JunipayService — RS256 auth header construction, provider mapping,
pesewas->decimal conversion, and error-status handling. No real JuniPay
calls (their docs warn test payments debit real money and there's no
sandbox), so httpx and JWT signing are both exercised against a throwaway
key, never the real one in secrets/.
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import jwt

from app.services.junipay_service import JunipayService

_TEST_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_TEST_PEM = _TEST_KEY.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()


def _service() -> JunipayService:
    return JunipayService("test-client", _TEST_PEM)


class FakeResponse:
    def __init__(self, body: dict, status_code: int = 200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


class TestAuthHeaders:
    def test_headers_carry_client_id_and_valid_rs256_token(self):
        headers = _service()._headers()
        assert headers["clientid"] == "test-client"
        assert headers["Authorization"].startswith("Bearer ")

        token = headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(token, _TEST_KEY.public_key(), algorithms=["RS256"])
        assert claims["clientId"] == "test-client"

    def test_escaped_newlines_in_stored_pem_are_restored(self):
        escaped = _TEST_PEM.replace("\n", "\\n")
        svc = JunipayService("test-client", escaped)
        headers = svc._headers()  # would raise if the key didn't reparse correctly
        assert headers["Authorization"].startswith("Bearer ")


@pytest.mark.asyncio
class TestChargeMobileMoney:
    async def test_amount_converted_pesewas_to_decimal_ghs(self):
        captured = {}

        async def fake_post(self, url, headers=None, json=None, timeout=None):
            captured["json"] = json
            return FakeResponse({"status": "pending", "transID": "COL1", "foreignID": json["foreignID"]})

        with patch.object(httpx.AsyncClient, "post", new=fake_post):
            await _service().charge_mobile_money(
                email="ussd@pollord.vote",
                amount=150,
                reference="vote_ussd_abc",
                phone="0557079838",
                provider="mtn",
                callback_url="https://server.pollord.com/api/v1/payments/junipay/webhook",
            )

        assert captured["json"]["amount"] == 1.50
        assert captured["json"]["tot_amnt"] == 1.50

    @pytest.mark.parametrize(
        "our_id,junipay_id", [("mtn", "mtn"), ("vod", "vodafone"), ("atl", "airteltigo")]
    )
    async def test_provider_mapped_to_junipay_identifier(self, our_id, junipay_id):
        captured = {}

        async def fake_post(self, url, headers=None, json=None, timeout=None):
            captured["provider"] = json["provider"]
            return FakeResponse({"status": "pending", "transID": "COL1", "foreignID": "ref"})

        with patch.object(httpx.AsyncClient, "post", new=fake_post):
            await _service().charge_mobile_money(
                email="a@b.com", amount=100, reference="ref", phone="0557079838",
                provider=our_id, callback_url="https://x/webhook",
            )
        assert captured["provider"] == junipay_id

    async def test_failed_status_raises_4xx(self):
        with patch.object(
            httpx.AsyncClient, "post",
            new=AsyncMock(return_value=FakeResponse({"status": "failed", "message": "Insufficient funds"})),
        ):
            with pytest.raises(HTTPException) as exc:
                await _service().charge_mobile_money(
                    email="a@b.com", amount=100, reference="ref", phone="0557079838",
                    provider="mtn", callback_url="https://x/webhook",
                )
        assert 400 <= exc.value.status_code < 500
        assert "Insufficient funds" in exc.value.detail

    async def test_http_error_status_raises_4xx(self):
        with patch.object(
            httpx.AsyncClient, "post",
            new=AsyncMock(return_value=FakeResponse({"message": "Unauthorized"}, status_code=401)),
        ):
            with pytest.raises(HTTPException) as exc:
                await _service().charge_mobile_money(
                    email="a@b.com", amount=100, reference="ref", phone="0557079838",
                    provider="mtn", callback_url="https://x/webhook",
                )
        assert 400 <= exc.value.status_code < 500


@pytest.mark.asyncio
class TestCheckTransactionStatus:
    async def test_returns_nested_info_object(self):
        info = {"status": "SUCCESS", "transID": "COL1", "foreignID": "ref"}
        with patch.object(
            httpx.AsyncClient, "post", new=AsyncMock(return_value=FakeResponse({"code": 200, "status": "SUCCESS", "info": info})),
        ):
            result = await _service().check_transaction_status("COL1")
        assert result == info

    async def test_http_error_raises(self):
        with patch.object(
            httpx.AsyncClient, "post",
            new=AsyncMock(return_value=FakeResponse({"message": "not found"}, status_code=404)),
        ):
            with pytest.raises(HTTPException):
                await _service().check_transaction_status("bogus")
