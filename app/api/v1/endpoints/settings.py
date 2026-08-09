from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_db
from app.models.platform_setting import PlatformSetting
from app.schemas.settings import PublicLaunchStatus

router = APIRouter(prefix="/settings", tags=["Settings"])

# Independent from admin.py's PLATFORM_SETTING_DEFAULTS by design (same pattern
# as elections.py's _get_global_vote_price falling back to settings.VOTE_PRICE
# rather than importing admin.py's dict) — but the *value* must match Task 1's
# "launch_at" default exactly: 2026-08-13T00:00:00+00:00.
DEFAULT_LAUNCH_GATE_ENABLED = True
DEFAULT_LAUNCH_AT = datetime(2026, 8, 13, tzinfo=timezone.utc)


async def _get_setting_value(db: AsyncSession, key: str) -> str | None:
    result = await db.execute(select(PlatformSetting).where(PlatformSetting.key == key))
    row = result.scalar_one_or_none()
    return row.value if row else None


@router.get("/launch", response_model=PublicLaunchStatus)
async def get_launch_status(db: AsyncSession = Depends(get_db)):
    gate_value = await _get_setting_value(db, "launch_gate_enabled")
    launch_at_value = await _get_setting_value(db, "launch_at")

    gate_enabled = (
        gate_value.lower() == "true" if gate_value is not None else DEFAULT_LAUNCH_GATE_ENABLED
    )
    launch_at = (
        datetime.fromisoformat(launch_at_value) if launch_at_value else DEFAULT_LAUNCH_AT
    )

    return PublicLaunchStatus(gate_enabled=gate_enabled, launch_at=launch_at)
