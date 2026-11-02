"""CRUD access for BackupRecord."""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.backup_record import BackupRecord


class BackupRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def create(self, **kwargs) -> BackupRecord:
        record = BackupRecord(**kwargs)
        self._db.add(record)
        await self._db.flush()
        await self._db.refresh(record)
        return record

    async def get(self, backup_id: uuid.UUID) -> Optional[BackupRecord]:
        result = await self._db.execute(
            select(BackupRecord).where(BackupRecord.backup_id == backup_id)
        )
        return result.scalar_one_or_none()

    async def list_recent(self, limit: int = 50, offset: int = 0) -> list[BackupRecord]:
        result = await self._db.execute(
            select(BackupRecord)
            .order_by(desc(BackupRecord.created_at))
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count(self) -> int:
        result = await self._db.execute(select(func.count()).select_from(BackupRecord))
        return result.scalar_one()

    async def stats(self) -> dict:
        result = await self._db.execute(
            select(
                func.count().label("total"),
                func.count().filter(BackupRecord.status == "completed").label("completed"),
                func.count().filter(BackupRecord.status == "failed").label("failed"),
                func.coalesce(func.sum(BackupRecord.size_bytes).filter(
                    BackupRecord.status == "completed"
                ), 0).label("storage_bytes"),
                func.max(BackupRecord.completed_at).filter(
                    BackupRecord.status == "completed"
                ).label("last_completed_at"),
            ).select_from(BackupRecord)
        )
        row = result.one()
        return {
            "total": row.total,
            "completed": row.completed,
            "failed": row.failed,
            "storage_bytes": int(row.storage_bytes),
            "last_completed_at": row.last_completed_at,
        }

    async def update(self, backup_id: uuid.UUID, **fields) -> Optional[BackupRecord]:
        record = await self.get(backup_id)
        if not record:
            return None
        for k, v in fields.items():
            setattr(record, k, v)
        await self._db.flush()
        await self._db.refresh(record)
        return record

    async def delete(self, backup_id: uuid.UUID) -> bool:
        record = await self.get(backup_id)
        if not record:
            return False
        await self._db.delete(record)
        await self._db.flush()
        return True

    async def list_stale(self, older_than: datetime) -> list[BackupRecord]:
        """Return pending/running records whose created_at is before older_than."""
        result = await self._db.execute(
            select(BackupRecord).where(
                BackupRecord.status.in_(["pending", "running"]),
                BackupRecord.created_at < older_than,
            )
        )
        return list(result.scalars().all())

    async def list_completed_oldest_first(self, type_: Optional[str] = None) -> list[BackupRecord]:
        q = (
            select(BackupRecord)
            .where(BackupRecord.status == "completed")
            .order_by(BackupRecord.created_at)
        )
        if type_:
            q = q.where(BackupRecord.type == type_)
        result = await self._db.execute(q)
        return list(result.scalars().all())
