"""TOTP 2FA: setup/enable/disable/regenerate, and the real login-flow
challenge (login() pauses for a 2FA-enabled account; /auth/2fa/login-verify
completes it) — not just the account-management CRUD surface."""
import pyotp
import pytest
from httpx import AsyncClient


async def _enable_2fa(client: AsyncClient, headers: dict) -> tuple[str, list[str]]:
    setup = await client.post("/api/v1/users/me/2fa/setup", headers=headers)
    assert setup.status_code == 200
    secret = setup.json()["secret"]

    code = pyotp.TOTP(secret).now()
    enable = await client.post("/api/v1/users/me/2fa/enable", json={"code": code}, headers=headers)
    assert enable.status_code == 200, enable.text
    return secret, enable.json()["recovery_codes"]


@pytest.mark.asyncio
class TestTwoFactorSetup:
    async def test_setup_returns_secret_and_uri(self, client: AsyncClient, test_user):
        res = await client.post("/api/v1/users/me/2fa/setup", headers=test_user["headers"])
        assert res.status_code == 200
        body = res.json()
        assert len(body["secret"]) >= 16
        assert body["secret"] in body["otpauth_uri"]

    async def test_enable_with_invalid_code_rejected(self, client: AsyncClient, test_user):
        await client.post("/api/v1/users/me/2fa/setup", headers=test_user["headers"])
        res = await client.post(
            "/api/v1/users/me/2fa/enable", json={"code": "000000"}, headers=test_user["headers"]
        )
        assert res.status_code == 400

    async def test_enable_with_valid_code_returns_eight_recovery_codes(
        self, client: AsyncClient, test_user
    ):
        _, codes = await _enable_2fa(client, test_user["headers"])
        assert len(codes) == 8
        assert len(set(codes)) == 8


@pytest.mark.asyncio
class TestTwoFactorLoginChallenge:
    async def test_login_requires_2fa_once_enabled(self, client: AsyncClient, test_user):
        await _enable_2fa(client, test_user["headers"])

        res = await client.post(
            "/api/v1/auth/login", json={"email": "test@example.com", "password": "Test1234!"}
        )
        assert res.status_code == 200
        body = res.json()
        assert body["requires_2fa"] is True
        assert body["mfa_token"]
        assert body.get("access_token") is None

    async def test_verify_with_totp_code_completes_login(self, client: AsyncClient, test_user):
        secret, _ = await _enable_2fa(client, test_user["headers"])

        login = await client.post(
            "/api/v1/auth/login", json={"email": "test@example.com", "password": "Test1234!"}
        )
        mfa_token = login.json()["mfa_token"]

        verify = await client.post(
            "/api/v1/auth/2fa/login-verify",
            json={"mfa_token": mfa_token, "code": pyotp.TOTP(secret).now()},
        )
        assert verify.status_code == 200, verify.text
        assert "access_token" in verify.json()

    async def test_verify_with_wrong_code_rejected(self, client: AsyncClient, test_user):
        await _enable_2fa(client, test_user["headers"])
        login = await client.post(
            "/api/v1/auth/login", json={"email": "test@example.com", "password": "Test1234!"}
        )
        mfa_token = login.json()["mfa_token"]

        verify = await client.post(
            "/api/v1/auth/2fa/login-verify", json={"mfa_token": mfa_token, "code": "000000"}
        )
        assert verify.status_code == 401

    async def test_recovery_code_completes_login_and_is_single_use(
        self, client: AsyncClient, test_user
    ):
        _, codes = await _enable_2fa(client, test_user["headers"])
        recovery_code = codes[0]

        login = await client.post(
            "/api/v1/auth/login", json={"email": "test@example.com", "password": "Test1234!"}
        )
        mfa_token = login.json()["mfa_token"]

        first = await client.post(
            "/api/v1/auth/2fa/login-verify", json={"mfa_token": mfa_token, "code": recovery_code}
        )
        assert first.status_code == 200, first.text

        # Same recovery code, a fresh challenge — must not work twice.
        login2 = await client.post(
            "/api/v1/auth/login", json={"email": "test@example.com", "password": "Test1234!"}
        )
        mfa_token2 = login2.json()["mfa_token"]
        second = await client.post(
            "/api/v1/auth/2fa/login-verify", json={"mfa_token": mfa_token2, "code": recovery_code}
        )
        assert second.status_code == 401


@pytest.mark.asyncio
class TestTwoFactorDisableAndRegenerate:
    async def test_disable_requires_correct_password(self, client: AsyncClient, test_user):
        await _enable_2fa(client, test_user["headers"])
        res = await client.post(
            "/api/v1/users/me/2fa/disable", json={"password": "WrongPassword1!"}, headers=test_user["headers"]
        )
        assert res.status_code == 400

    async def test_disable_then_login_no_longer_challenges(self, client: AsyncClient, test_user):
        await _enable_2fa(client, test_user["headers"])
        disable = await client.post(
            "/api/v1/users/me/2fa/disable", json={"password": "Test1234!"}, headers=test_user["headers"]
        )
        assert disable.status_code == 204

        login = await client.post(
            "/api/v1/auth/login", json={"email": "test@example.com", "password": "Test1234!"}
        )
        assert login.json()["requires_2fa"] is False
        assert login.json()["access_token"]

    async def test_regenerate_recovery_codes_invalidates_old_ones(
        self, client: AsyncClient, test_user
    ):
        _, old_codes = await _enable_2fa(client, test_user["headers"])

        regen = await client.post(
            "/api/v1/users/me/2fa/recovery-codes/regenerate",
            json={"password": "Test1234!"},
            headers=test_user["headers"],
        )
        assert regen.status_code == 200
        new_codes = regen.json()["recovery_codes"]
        assert set(new_codes).isdisjoint(old_codes)

        login = await client.post(
            "/api/v1/auth/login", json={"email": "test@example.com", "password": "Test1234!"}
        )
        mfa_token = login.json()["mfa_token"]
        old_code_attempt = await client.post(
            "/api/v1/auth/2fa/login-verify", json={"mfa_token": mfa_token, "code": old_codes[0]}
        )
        assert old_code_attempt.status_code == 401
