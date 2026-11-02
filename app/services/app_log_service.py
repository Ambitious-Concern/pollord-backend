import hashlib
import os
import re
import traceback as tb_module
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_log import AppLog
from app.repositories.app_log_repository import AppLogRepository
from app.schemas.app_log import (
    AppLogListResponse,
    AppLogResponse,
    AppLogStats,
    AppLogTrendBucket,
    AppLogTrendResponse,
    SuggestedFix,
)

# ── Suggested-fix pattern library ─────────────────────────────────────────────

_FIX_RULES: list[tuple[str, str, str, float]] = [
    (
        r"unique.*constraint|duplicate.*key|UniqueViolation|IntegrityError.*unique",
        "Duplicate entry violation",
        "Add a uniqueness check before the insert, or use INSERT … ON CONFLICT DO NOTHING / DO UPDATE SET … to handle races atomically.",
        0.9,
    ),
    (
        r"foreign.*key.*constraint|ForeignKeyViolation",
        "Foreign key constraint failed",
        "Verify the parent record exists before inserting the child, or wrap in try/except and return HTTP 422.",
        0.9,
    ),
    (
        r"timeout|StatementTimeout|QueryTimeout|statement timeout",
        "Query or connection timeout",
        "Add a database index on the filtered column, break large queries into smaller batches, or raise statement_timeout for this path.",
        0.85,
    ),
    (
        r"ConnectionRefused|Connection refused|ECONNREFUSED",
        "Service unreachable",
        "Check connection pool size, verify the dependent service is running, and add a circuit-breaker or retry with exponential back-off.",
        0.9,
    ),
    (
        r"NoneType.*has no attribute|AttributeError.*None|object has no attribute",
        "Null reference (NoneType)",
        "Add a None guard (`if obj is None: raise HTTPException(404)`) or use Optional chaining before accessing the attribute.",
        0.85,
    ),
    (
        r"KeyError",
        "Missing dictionary key",
        "Use `.get(key, default)` instead of direct bracket access, or validate the key exists with an `in` check first.",
        0.7,
    ),
    (
        r"ValidationError|pydantic.*error",
        "Pydantic schema validation failed",
        "Review the input schema and ensure the calling code sends all required fields with correct types. Add a custom validator if the rule is complex.",
        0.85,
    ),
    (
        r"401|Unauthorized|invalid.*token|expired.*token|jwt",
        "Authentication failure",
        "Check that the JWT secret hasn't rotated and that refresh-token rotation is working. Verify token expiry is not too short.",
        0.65,
    ),
    (
        r"403|Forbidden|permission.*denied|not.*authoris",
        "Authorisation failure",
        "Confirm the required role is assigned to the user and that the `require_roles` decorator on the endpoint is correct.",
        0.65,
    ),
    (
        r"429|rate.*limit|too.*many.*request",
        "Rate limit exceeded",
        "Raise the rate limit for legitimate high-volume paths, or add Redis caching to reduce upstream request load.",
        0.7,
    ),
    (
        r"celery|task.*failed|Retry|MaxRetriesExceeded",
        "Celery task failure",
        "Check the Celery worker logs for the full traceback. Add retry logic with exponential back-off (`max_retries`, `countdown`).",
        0.6,
    ),
    (
        r"redis.*error|Redis.*connection|ConnectionError.*redis|aioredis",
        "Redis connection error",
        "Verify Redis is running and REDIS_URL is correct. Check connection pool exhaustion and increase `max_connections` if needed.",
        0.85,
    ),
    (
        r"smtp|email.*error|EmailError|SMTPException",
        "Email delivery failure",
        "Verify SMTP credentials, check if the recipient domain is blocked, and add exponential back-off retries.",
        0.65,
    ),
    (
        r"IndexError|list index out of range",
        "List index out of range",
        "Guard array access with a length check, or use `.get()` / slicing with defaults instead of direct index access.",
        0.7,
    ),
    (
        r"MemoryError|out of memory",
        "Out-of-memory condition",
        "Stream large datasets instead of loading them fully, add pagination, and increase container memory limits if needed.",
        0.85,
    ),
]


def suggested_fixes(message: str, traceback: Optional[str]) -> list[SuggestedFix]:
    text = (message + " " + (traceback or "")).lower()
    found = []
    for pattern, title, detail, confidence in _FIX_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            found.append(SuggestedFix(title=title, detail=detail, confidence=confidence))
    if not found:
        found.append(
            SuggestedFix(
                title="Review traceback carefully",
                detail="No automated pattern matched. Read the full traceback, add targeted try/except with logging, and add a test for the failing case.",
                confidence=0.2,
            )
        )
    return found


# ── Fingerprint ────────────────────────────────────────────────────────────────

def _fingerprint(level: str, source: str, message: str, traceback: Optional[str]) -> str:
    first_tb_line = ""
    if traceback:
        lines = [l.strip() for l in traceback.splitlines() if l.strip() and "File" in l]
        first_tb_line = lines[-1] if lines else ""
    raw = f"{level}:{source}:{message[:120]}:{first_tb_line}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── AppLog enrichment ──────────────────────────────────────────────────────────

def _enrich(log: AppLog, affected_users_count: int = 0) -> AppLogResponse:
    resp = AppLogResponse.model_validate(log)
    resp.suggested_fixes = suggested_fixes(log.message, log.traceback)
    resp.affected_users_count = affected_users_count
    return resp


# ── Capture helper (called from exception handlers) ───────────────────────────

async def capture_exception(
    exc: Exception,
    db: AsyncSession,
    request: Optional[Request] = None,
    level: str = "error",
    extra: Optional[dict] = None,
) -> None:
    """Write an AppLog row for an unhandled exception. Fire-and-forget safe."""
    try:
        tb_str = "".join(tb_module.format_exception(type(exc), exc, exc.__traceback__))

        source = type(exc).__name__
        tb = exc.__traceback__
        if tb:
            while tb.tb_next:
                tb = tb.tb_next
            frame = tb.tb_frame
            source = f"{frame.f_code.co_filename.split('app/')[-1]}:{tb.tb_lineno}"

        logger_name = type(exc).__module__ or "app"
        message = str(exc) or type(exc).__name__
        fp = _fingerprint(level, source, message, tb_str)
        release = os.environ.get("CURRENT_RELEASE")

        request_method = request_path = request_ip = request_user_email = request_id = None
        http_status = 500
        if request:
            request_method = request.method
            request_path = str(request.url.path)
            request_ip = request.client.host if request.client else None
            request_id = request.headers.get("x-request-id")

        repo = AppLogRepository(db)
        now_naive = datetime.utcnow()
        await repo.bump_or_create(
            fingerprint=fp,
            level=level,
            source=source,
            logger_name=logger_name,
            message=message,
            traceback=tb_str,
            request_method=request_method,
            request_path=request_path,
            request_ip=request_ip,
            request_user_email=request_user_email,
            request_id=request_id,
            http_status=http_status,
            extra=extra,
            release=release,
            first_seen_at=now_naive,
            last_seen_at=now_naive,
        )
        await db.commit()
    except Exception:
        pass


# ── Service ────────────────────────────────────────────────────────────────────

class AppLogService:
    def __init__(self, db: AsyncSession):
        self.repo = AppLogRepository(db)

    async def get_stats(self) -> AppLogStats:
        data = await self.repo.stats()
        return AppLogStats(**data)

    async def list_logs(
        self,
        level: Optional[str],
        resolved: Optional[bool],
        search: Optional[str],
        source: Optional[str],
        logger_name: Optional[str],
        path_prefix: Optional[str],
        release: Optional[str],
        limit: int,
        offset: int,
    ) -> AppLogListResponse:
        rows, total = await self.repo.list(
            level=level, resolved=resolved, search=search,
            source=source, logger_name=logger_name,
            path_prefix=path_prefix, release=release,
            limit=limit, offset=offset,
        )
        return AppLogListResponse(items=[_enrich(r) for r in rows], total=total)

    async def list_issues(
        self,
        level: Optional[str],
        resolved: Optional[bool],
        search: Optional[str],
        source: Optional[str],
        logger_name: Optional[str],
        path_prefix: Optional[str],
        release: Optional[str],
        limit: int,
        offset: int,
    ) -> AppLogListResponse:
        rows, users_map, total = await self.repo.list_issues(
            level=level, resolved=resolved, search=search,
            source=source, logger_name=logger_name,
            path_prefix=path_prefix, release=release,
            limit=limit, offset=offset,
        )
        return AppLogListResponse(
            items=[_enrich(r, users_map.get(r.fingerprint, 0)) for r in rows],
            total=total,
        )

    async def get_log(self, log_id: uuid.UUID) -> Optional[AppLogResponse]:
        log = await self.repo.get(log_id)
        return _enrich(log) if log else None

    async def trend(self, hours: int = 24) -> AppLogTrendResponse:
        rows = await self.repo.trend(hours)
        return AppLogTrendResponse(
            buckets=[AppLogTrendBucket(**r) for r in rows]
        )

    async def resolve(self, log_id: uuid.UUID, by_email: str) -> Optional[AppLogResponse]:
        log = await self.repo.resolve(log_id, by_email)
        return _enrich(log) if log else None

    async def resolve_issue(self, fingerprint: str, by_email: str) -> int:
        return await self.repo.resolve_all_by_fingerprint(fingerprint, by_email)

    async def delete(self, log_id: uuid.UUID) -> bool:
        return await self.repo.delete(log_id)
