"""Realistic active sessions + recent login activity for the seed admin, so
Settings > Sessions and Settings > Login Activity have real rows to show
without requiring an actual login first."""
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.refresh_token import RefreshToken
from app.models.user import User
from seeders import config

# (user_agent, ip) pairs — a desktop browser and a phone, both plausible
# Ghanaian mobile-network IP ranges, so the two rows read as two real devices.
_DEVICES = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36",
        "154.160.22.41",
    ),
    (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
        "197.251.199.14",
    ),
]


async def seed_admin_sessions(db: AsyncSession, admin: User) -> None:
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.user_id == admin.user_id, RefreshToken.revoked.is_(False))
    )
    if len(result.scalars().all()) >= config.N_ADMIN_SESSIONS:
        return

    now = datetime.now(timezone.utc)
    for i in range(config.N_ADMIN_SESSIONS):
        user_agent, ip = _DEVICES[i % len(_DEVICES)]
        db.add(RefreshToken(
            jti=secrets.token_hex(32),
            user_id=admin.user_id,
            expires_at=now + timedelta(days=30 - i),
            revoked=False,
            ip_address=ip,
            user_agent=user_agent,
            created_at=now - timedelta(days=i + 1, hours=3),
        ))
    await db.flush()


async def seed_admin_login_activity(db: AsyncSession, admin: User) -> None:
    result = await db.execute(
        select(AuditLog).where(AuditLog.user_id == admin.user_id, AuditLog.action_type == "LOGIN")
    )
    if len(result.scalars().all()) >= config.N_ADMIN_LOGIN_ACTIVITIES:
        return

    now = datetime.now(timezone.utc)
    for i in range(config.N_ADMIN_LOGIN_ACTIVITIES):
        user_agent, ip = _DEVICES[i % len(_DEVICES)]
        db.add(AuditLog(
            user_id=admin.user_id,
            action_type="LOGIN",
            entity_type="User",
            entity_id=admin.user_id,
            ip_address=ip,
            user_agent=user_agent,
            created_at=now - timedelta(days=i + 1, hours=3),
        ))
    await db.flush()
