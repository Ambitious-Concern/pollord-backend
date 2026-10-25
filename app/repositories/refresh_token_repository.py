from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.refresh_token import RefreshToken
from app.repositories.base import BaseRepository


class RefreshTokenRepository(BaseRepository[RefreshToken]):
    def __init__(self, model, session: AsyncSession):
        super().__init__(model, session)

    async def get_active_for_user(self, user_id: UUID) -> List[RefreshToken]:
        """Every currently-usable session — each device holds exactly one
        non-revoked row at a time (rotation revokes the old one and creates
        a fresh row), so this list IS the active-session list."""
        result = await self.session.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked.is_(False),
                RefreshToken.expires_at > datetime.now(timezone.utc),
            ).order_by(RefreshToken.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_by_jti(self, jti: str) -> Optional[RefreshToken]:
        result = await self.session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
        return result.scalar_one_or_none()

    async def revoke(self, jti: str) -> bool:
        token = await self.get_by_jti(jti)
        if not token:
            return False
        token.revoked = True
        await self.session.flush()
        return True
