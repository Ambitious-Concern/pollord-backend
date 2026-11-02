from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import distinct, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_log import AppLog


class AppLogRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, **kwargs) -> AppLog:
        log = AppLog(**kwargs)
        self.db.add(log)
        await self.db.flush()
        return log

    async def bump_or_create(self, fingerprint: str, **kwargs) -> AppLog:
        """Increment occurrence_count if fingerprint seen recently, else create new."""
        result = await self.db.execute(
            select(AppLog)
            .where(AppLog.fingerprint == fingerprint, AppLog.resolved == False)
            .order_by(AppLog.created_at.desc())
            .limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.occurrence_count += 1
            existing.last_seen_at = datetime.utcnow()
            await self.db.flush()
            return existing
        return await self.create(fingerprint=fingerprint, **kwargs)

    async def get(self, log_id: uuid.UUID) -> Optional[AppLog]:
        result = await self.db.execute(select(AppLog).where(AppLog.log_id == log_id))
        return result.scalar_one_or_none()

    def _apply_filters(self, q, *, level, resolved, search, source, logger_name, path_prefix, release):
        if level:
            q = q.where(AppLog.level == level)
        if resolved is not None:
            q = q.where(AppLog.resolved == resolved)
        if search:
            term = f"%{search}%"
            q = q.where(
                AppLog.message.ilike(term)
                | AppLog.source.ilike(term)
                | AppLog.request_path.ilike(term)
            )
        if source:
            q = q.where(AppLog.source.ilike(f"%{source}%"))
        if logger_name:
            q = q.where(AppLog.logger_name.ilike(f"%{logger_name}%"))
        if path_prefix:
            q = q.where(AppLog.request_path.ilike(f"{path_prefix}%"))
        if release:
            q = q.where(AppLog.release == release)
        return q

    async def list(
        self,
        level: Optional[str] = None,
        resolved: Optional[bool] = None,
        search: Optional[str] = None,
        source: Optional[str] = None,
        logger_name: Optional[str] = None,
        path_prefix: Optional[str] = None,
        release: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AppLog], int]:
        q = self._apply_filters(
            select(AppLog),
            level=level, resolved=resolved, search=search,
            source=source, logger_name=logger_name,
            path_prefix=path_prefix, release=release,
        )
        count_q = select(func.count()).select_from(q.subquery())
        total = (await self.db.execute(count_q)).scalar_one()
        rows = (
            await self.db.execute(q.order_by(AppLog.created_at.desc()).limit(limit).offset(offset))
        ).scalars().all()
        return list(rows), total

    async def list_issues(
        self,
        level: Optional[str] = None,
        resolved: Optional[bool] = None,
        search: Optional[str] = None,
        source: Optional[str] = None,
        logger_name: Optional[str] = None,
        path_prefix: Optional[str] = None,
        release: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AppLog], dict[str, int], int]:
        """Return the latest representative row per fingerprint + affected_users_count map."""
        # Subquery: max(last_seen_at) per fingerprint — UUIDs can't use max()
        sub = select(
            AppLog.fingerprint.label("fp"),
            func.max(AppLog.last_seen_at).label("max_ts"),
        ).group_by(AppLog.fingerprint)
        if level:
            sub = sub.where(AppLog.level == level)
        if resolved is not None:
            sub = sub.where(AppLog.resolved == resolved)
        if release:
            sub = sub.where(AppLog.release == release)
        sub = sub.subquery()

        q = select(AppLog).join(
            sub,
            (AppLog.fingerprint == sub.c.fp) & (AppLog.last_seen_at == sub.c.max_ts),
        )
        if search:
            term = f"%{search}%"
            q = q.where(AppLog.message.ilike(term) | AppLog.source.ilike(term))
        if source:
            q = q.where(AppLog.source.ilike(f"%{source}%"))
        if logger_name:
            q = q.where(AppLog.logger_name.ilike(f"%{logger_name}%"))
        if path_prefix:
            q = q.where(AppLog.request_path.ilike(f"{path_prefix}%"))

        count_q = select(func.count()).select_from(q.subquery())
        total = (await self.db.execute(count_q)).scalar_one()
        rows = (
            await self.db.execute(q.order_by(AppLog.last_seen_at.desc()).limit(limit).offset(offset))
        ).scalars().all()

        # Compute affected_users_count per fingerprint in one query
        fingerprints = [r.fingerprint for r in rows]
        users_map: dict[str, int] = {}
        if fingerprints:
            users_q = (
                select(AppLog.fingerprint, func.count(distinct(AppLog.request_user_email)).label("users"))
                .where(AppLog.fingerprint.in_(fingerprints), AppLog.request_user_email.isnot(None))
                .group_by(AppLog.fingerprint)
            )
            for fp, cnt in (await self.db.execute(users_q)).all():
                users_map[fp] = cnt

        return list(rows), users_map, total

    async def stats(self) -> dict:
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        total = (await self.db.execute(select(func.count(AppLog.log_id)))).scalar_one()
        critical = (
            await self.db.execute(select(func.count()).where(AppLog.level == "critical"))
        ).scalar_one()
        errors = (
            await self.db.execute(select(func.count()).where(AppLog.level == "error"))
        ).scalar_one()
        warnings = (
            await self.db.execute(select(func.count()).where(AppLog.level == "warning"))
        ).scalar_one()
        unresolved = (
            await self.db.execute(select(func.count()).where(AppLog.resolved == False))
        ).scalar_one()
        resolved_today = (
            await self.db.execute(
                select(func.count()).where(
                    AppLog.resolved == True, AppLog.resolved_at >= today_start
                )
            )
        ).scalar_one()
        return {
            "total": total,
            "critical": critical,
            "errors": errors,
            "warnings": warnings,
            "unresolved": unresolved,
            "resolved_today": resolved_today,
        }

    async def trend(self, hours: int = 24) -> list[dict]:
        """Hourly log counts by level for the last N hours."""
        since = datetime.utcnow() - timedelta(hours=hours)
        result = await self.db.execute(
            text("""
                SELECT
                    date_trunc('hour', created_at AT TIME ZONE 'UTC') AS hour,
                    level,
                    COUNT(*) AS count
                FROM app_logs
                WHERE created_at >= :since
                GROUP BY 1, 2
                ORDER BY 1
            """),
            {"since": since},
        )
        return [{"hour": row.hour, "level": row.level, "count": row.count} for row in result]

    async def list_since(self, since: datetime, limit: int = 50) -> list[AppLog]:
        """Return logs created after `since` for live tail."""
        result = await self.db.execute(
            select(AppLog)
            .where(AppLog.created_at > since)
            .order_by(AppLog.created_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def resolve(self, log_id: uuid.UUID, by_email: str) -> Optional[AppLog]:
        now = datetime.utcnow()
        await self.db.execute(
            update(AppLog)
            .where(AppLog.log_id == log_id)
            .values(resolved=True, resolved_at=now, resolved_by_email=by_email)
        )
        return await self.get(log_id)

    async def resolve_all_by_fingerprint(self, fingerprint: str, by_email: str) -> int:
        now = datetime.utcnow()
        result = await self.db.execute(
            update(AppLog)
            .where(AppLog.fingerprint == fingerprint, AppLog.resolved == False)
            .values(resolved=True, resolved_at=now, resolved_by_email=by_email)
        )
        return result.rowcount

    async def delete(self, log_id: uuid.UUID) -> bool:
        log = await self.get(log_id)
        if not log:
            return False
        await self.db.delete(log)
        return True
