from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.abuse_alert import AbuseAlert, AlertSeverity, AlertStatus, AlertType


class AbuseAlertRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_filtered(
        self,
        *,
        severity: Optional[AlertSeverity] = None,
        alert_type: Optional[AlertType] = None,
        status: Optional[AlertStatus] = None,
        since: Optional[datetime] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[AbuseAlert]:
        q = (
            select(AbuseAlert)
            .options(
                joinedload(AbuseAlert.source_user),
                joinedload(AbuseAlert.resolver),
            )
            .order_by(AbuseAlert.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        if severity:
            q = q.where(AbuseAlert.severity == severity)
        if alert_type:
            q = q.where(AbuseAlert.alert_type == alert_type)
        if status:
            q = q.where(AbuseAlert.status == status)
        if since:
            q = q.where(AbuseAlert.created_at >= since)
        result = await self.db.execute(q)
        return list(result.scalars().unique().all())

    async def get_by_id(self, alert_id: UUID) -> Optional[AbuseAlert]:
        q = (
            select(AbuseAlert)
            .options(
                joinedload(AbuseAlert.source_user),
                joinedload(AbuseAlert.resolver),
            )
            .where(AbuseAlert.id == alert_id)
        )
        result = await self.db.execute(q)
        return result.scalar_one_or_none()

    async def update_status(
        self,
        alert_id: UUID,
        status: AlertStatus,
        resolved_by_id: Optional[UUID] = None,
    ) -> Optional[AbuseAlert]:
        alert = await self.get_by_id(alert_id)
        if not alert:
            return None
        alert.status = status
        if status in (AlertStatus.RESOLVED, AlertStatus.DISMISSED):
            alert.resolved_at = datetime.now(timezone.utc)
            alert.resolved_by = resolved_by_id
        else:
            alert.resolved_at = None
            alert.resolved_by = None
        await self.db.commit()
        await self.db.refresh(alert)
        return alert

    async def get_stats(self) -> dict:
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        total_result = await self.db.execute(select(func.count(AbuseAlert.id)))
        total = total_result.scalar_one()

        critical_result = await self.db.execute(
            select(func.count(AbuseAlert.id)).where(
                AbuseAlert.severity == AlertSeverity.CRITICAL
            )
        )
        critical = critical_result.scalar_one()

        unresolved_result = await self.db.execute(
            select(func.count(AbuseAlert.id)).where(
                AbuseAlert.status.in_([AlertStatus.NEW, AlertStatus.INVESTIGATING])
            )
        )
        unresolved = unresolved_result.scalar_one()

        resolved_today_result = await self.db.execute(
            select(func.count(AbuseAlert.id)).where(
                AbuseAlert.status == AlertStatus.RESOLVED,
                AbuseAlert.resolved_at >= today_start,
            )
        )
        resolved_today = resolved_today_result.scalar_one()

        return {
            "total": total,
            "critical": critical,
            "unresolved": unresolved,
            "resolved_today": resolved_today,
        }
