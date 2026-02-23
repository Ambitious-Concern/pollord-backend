import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestRegistration:
    async def test_register_success(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "newuser@example.com",
                "password": "NewUser123!",
                "full_name": "New User",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["email"] == "newuser@example.com"
        assert data["full_name"] == "New User"
        assert "Voter" in data["roles"]

    async def test_register_duplicate_email(self, client: AsyncClient, test_user):
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "test@example.com",
                "password": "Duplicate123!",
                "full_name": "Duplicate",
            },
        )
        assert response.status_code == 409

    async def test_register_weak_password(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "weak@example.com",
                "password": "weak",
                "full_name": "Weak Password",
            },
        )
        assert response.status_code == 422


@pytest.mark.asyncio
class TestLogin:
    async def test_login_success(self, client: AsyncClient, test_user):
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "test@example.com",
                "password": "Test1234!",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"

    async def test_login_wrong_password(self, client: AsyncClient, test_user):
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "test@example.com",
                "password": "WrongPassword1!",
            },
        )
        assert response.status_code == 401

    async def test_login_nonexistent_user(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "nobody@example.com",
                "password": "NoUser123!",
            },
        )
        assert response.status_code == 401


@pytest.mark.asyncio
class TestRefreshToken:
    async def test_refresh_success(self, client: AsyncClient, test_user):
        # Login first
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "test@example.com", "password": "Test1234!"},
        )
        refresh_token = login_resp.json()["refresh_token"]

        response = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        assert response.status_code == 200
        assert "access_token" in response.json()

    async def test_refresh_invalid_token(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": "invalid"},
        )
        assert response.status_code == 401


@pytest.mark.asyncio
class TestProtectedEndpoints:
    async def test_access_without_token(self, client: AsyncClient):
        response = await client.get("/api/v1/users/me")
        assert response.status_code == 401

    async def test_access_with_token(self, client: AsyncClient, test_user):
        response = await client.get(
            "/api/v1/users/me", headers=test_user["headers"]
        )
        assert response.status_code == 200
        assert response.json()["email"] == "test@example.com"
