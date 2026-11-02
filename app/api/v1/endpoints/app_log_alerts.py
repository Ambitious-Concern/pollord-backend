import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_roles
from app.db.base import get_db
from app.schemas.app_log_alert import AppLogAlertCreate, AppLogAlertResponse, AppLogAlertUpdate
from app.services.app_log_alert_service import AppLogAlertService

router = APIRouter(prefix="/admin/app-log-alerts", tags=["app-log-alerts"])

REQUIRED_ROLES = ("System Administrator",)


def _svc(db: AsyncSession = Depends(get_db)) -> AppLogAlertService:
    return AppLogAlertService(db)


@router.get("/", response_model=list[AppLogAlertResponse])
async def list_alerts(
    _admin=Depends(require_roles(*REQUIRED_ROLES)),
    svc: AppLogAlertService = Depends(_svc),
):
    return await svc.list_alerts()


@router.post("/", response_model=AppLogAlertResponse, status_code=201)
async def create_alert(
    body: AppLogAlertCreate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_roles(*REQUIRED_ROLES)),
    svc: AppLogAlertService = Depends(_svc),
):
    alert = await svc.create_alert(body)
    await db.commit()
    return alert


@router.get("/{alert_id}", response_model=AppLogAlertResponse)
async def get_alert(
    alert_id: uuid.UUID,
    _admin=Depends(require_roles(*REQUIRED_ROLES)),
    svc: AppLogAlertService = Depends(_svc),
):
    alert = await svc.get_alert(alert_id)
    if not alert:
        raise HTTPException(404, "Alert not found")
    return alert


@router.patch("/{alert_id}", response_model=AppLogAlertResponse)
async def update_alert(
    alert_id: uuid.UUID,
    body: AppLogAlertUpdate,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_roles(*REQUIRED_ROLES)),
    svc: AppLogAlertService = Depends(_svc),
):
    alert = await svc.update_alert(alert_id, body)
    if not alert:
        raise HTTPException(404, "Alert not found")
    await db.commit()
    return alert


@router.delete("/{alert_id}", status_code=204)
async def delete_alert(
    alert_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_roles(*REQUIRED_ROLES)),
    svc: AppLogAlertService = Depends(_svc),
):
    ok = await svc.delete_alert(alert_id)
    if not ok:
        raise HTTPException(404, "Alert not found")
    await db.commit()
