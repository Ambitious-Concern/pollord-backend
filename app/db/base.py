from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_size=20,
    max_overflow=10,
    connect_args={
        "server_settings": {"search_path": "public"},
        # Precaution for once DATABASE_URL points at PgBouncer in transaction
        # pool mode: asyncpg's prepared-statement cache assumes a stable
        # connection, but transaction pooling can hand a client a different
        # physical Postgres connection between statements. Verified under
        # concurrent load against pgbouncer 1.25 (the version this project's
        # compose file pulls) that its own prepared-statement tracking
        # already prevents the failure even without this — but that's
        # version-specific behavior, not a protocol guarantee, and disabling
        # the cache costs very little. Harmless when talking directly to
        # Postgres too.
        "statement_cache_size": 0,
    },
)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding one session per request; commits on
    success, rolls back if the request handler raises."""
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
