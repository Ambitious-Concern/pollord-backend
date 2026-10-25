"""The dashboard bell/panel's read side. Not org-tenant data (see
Notification's model docstring) — plain get_db, scoped by exact user_id
match in the repository, same as refresh_tokens."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_active_user
from app.db.base import get_db
from app.models.user import User
from app.schemas.notification import MarkAllReadResponse, NotificationListResponse
from app.services.notification_feed_service import NotificationFeedService

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    skip: int = 0,
    limit: int = 20,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    return await NotificationFeedService(db).list_for_user(current_user.user_id, skip=skip, limit=limit)


@router.post("/{notification_id}/read", status_code=204)
async def mark_notification_read(
    notification_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    marked = await NotificationFeedService(db).mark_read(notification_id, current_user.user_id)
    if not marked:
        raise HTTPException(status_code=404, detail="Notification not found")


@router.post("/read-all", response_model=MarkAllReadResponse)
async def mark_all_notifications_read(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    marked = await NotificationFeedService(db).mark_all_read(current_user.user_id)
    return MarkAllReadResponse(marked=marked)
