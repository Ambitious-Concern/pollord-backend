import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_log_alert import AppLogAlert


class AppLogAlertRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, **kwargs) -> AppLogAlert:
        alert = AppLogAlert(alert_id=uuid.uuid4(), **kwargs)
        self.db.add(alert)
        await self.db.flush()
        return alert

    async def get(self, alert_id: uuid.UUID) -> Optional[AppLogAlert]:
        result = await self.db.execute(
            select(AppLogAlert).where(AppLogAlert.alert_id == alert_id)
        )
        return result.scalar_one_or_none()

    async def list_all(self) -> list[AppLogAlert]:
        result = await self.db.execute(
            select(AppLogAlert).order_by(AppLogAlert.created_at.desc())
        )
        return list(result.scalars().all())

    async def list_enabled(self) -> list[AppLogAlert]:
        result = await self.db.execute(
            select(AppLogAlert).where(AppLogAlert.enabled == True)
        )
        return list(result.scalars().all())

    async def update(self, alert: AppLogAlert, **fields) -> AppLogAlert:
        for k, v in fields.items():
            setattr(alert, k, v)
        await self.db.flush()
        return alert

    async def mark_triggered(self, alert_id: uuid.UUID) -> None:
        alert = await self.get(alert_id)
        if alert:
            alert.last_triggered_at = datetime.now(timezone.utc)
            await self.db.flush()

    async def delete(self, alert_id: uuid.UUID) -> bool:
        alert = await self.get(alert_id)
        if not alert:
            return False
        await self.db.delete(alert)
        return True
