"""CRUD + basic evaluation for AppLogAlert rules."""
import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_log_alert import AppLogAlert
from app.repositories.app_log_alert_repository import AppLogAlertRepository
from app.schemas.app_log_alert import AppLogAlertCreate, AppLogAlertResponse, AppLogAlertUpdate


class AppLogAlertService:
    def __init__(self, db: AsyncSession):
        self.repo = AppLogAlertRepository(db)

    async def list_alerts(self) -> list[AppLogAlertResponse]:
        rows = await self.repo.list_all()
        return [AppLogAlertResponse.model_validate(r) for r in rows]

    async def create_alert(self, body: AppLogAlertCreate) -> AppLogAlertResponse:
        alert = await self.repo.create(**body.model_dump())
        return AppLogAlertResponse.model_validate(alert)

    async def get_alert(self, alert_id: uuid.UUID) -> Optional[AppLogAlertResponse]:
        alert = await self.repo.get(alert_id)
        return AppLogAlertResponse.model_validate(alert) if alert else None

    async def update_alert(
        self, alert_id: uuid.UUID, body: AppLogAlertUpdate
    ) -> Optional[AppLogAlertResponse]:
        alert = await self.repo.get(alert_id)
        if not alert:
            return None
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        updated = await self.repo.update(alert, **fields)
        return AppLogAlertResponse.model_validate(updated)

    async def delete_alert(self, alert_id: uuid.UUID) -> bool:
        return await self.repo.delete(alert_id)
