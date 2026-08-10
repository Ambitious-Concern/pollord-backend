import os
from typing import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import create_access_token, hash_password
from app.db.base import get_db
from app.models.base import Base

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://pollard:pollard@localhost:5432/pollard_test",
)

# NullPool prevents connection pool state from being shared across async event
# loop scopes, which avoids "Future attached to a different loop" errors with
# asyncpg under pytest-asyncio.
test_engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
test_session_maker = async_sessionmaker(
    test_engine, class_=AsyncSession, expire_on_commit=False
)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_database():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await test_engine.dispose()


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with test_session_maker() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    from app.main import app

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def test_user(db_session: AsyncSession) -> dict:
    from app.models.user import User, Role, UserRole
    from sqlalchemy import select

    user = User(
        email="test@example.com",
        password_hash=hash_password("Test1234!"),
        full_name="Test User",
        email_verified=True,
        account_status="active",
    )
    db_session.add(user)
    await db_session.flush()

    result = await db_session.execute(select(Role).where(Role.role_name == "Voter"))
    role = result.scalar_one_or_none()
    if not role:
        role = Role(role_name="Voter", permissions={"voting": ["cast", "read"]})
        db_session.add(role)
        await db_session.flush()

    user_role = UserRole(user_id=user.user_id, role_id=role.role_id)
    db_session.add(user_role)
    await db_session.flush()

    token = create_access_token(str(user.user_id))

    return {
        "user": user,
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> dict:
    from app.models.user import User, Role, UserRole
    from sqlalchemy import select

    user = User(
        email="admin@example.com",
        password_hash=hash_password("Admin1234!"),
        full_name="Admin User",
        email_verified=True,
        account_status="active",
    )
    db_session.add(user)
    await db_session.flush()

    result = await db_session.execute(
        select(Role).where(Role.role_name == "System Administrator")
    )
    role = result.scalar_one_or_none()
    if not role:
        role = Role(
            role_name="System Administrator",
            permissions={"admin": ["full"]},
        )
        db_session.add(role)
        await db_session.flush()

    user_role = UserRole(user_id=user.user_id, role_id=role.role_id)
    db_session.add(user_role)
    await db_session.flush()

    token = create_access_token(str(user.user_id))

    return {
        "user": user,
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }
