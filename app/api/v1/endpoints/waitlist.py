import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_roles
from app.db.base import get_db
from app.models.user import User
from app.models.waitlist import WaitlistSubscriber
from app.schemas.waitlist import (
    WaitlistAnnounceResponse,
    WaitlistSubscribeRequest,
    WaitlistSubscribeResponse,
)
from app.services.email_service import (
    launch_announcement_email,
    send_email,
    waitlist_confirmation_email,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/waitlist", tags=["Waitlist"])

ADMIN_ROLE = "System Administrator"


@router.post("/subscribe", response_model=WaitlistSubscribeResponse)
async def subscribe(
    data: WaitlistSubscribeRequest,
    db: AsyncSession = Depends(get_db),
):
    email = data.email.strip().lower()

    result = await db.execute(
        select(WaitlistSubscriber).where(WaitlistSubscriber.email == email)
    )
    if result.scalar_one_or_none():
        return WaitlistSubscribeResponse(message="You're already on the list!")

    db.add(WaitlistSubscriber(email=email))
    await db.commit()

    subject, html = waitlist_confirmation_email()
    send_email(email, subject, html)

    return WaitlistSubscribeResponse(
        message="You're on the list! Check your inbox for confirmation."
    )


@router.post("/announce", response_model=WaitlistAnnounceResponse)
async def announce(
    current_user: User = Depends(require_roles(ADMIN_ROLE)),
    db: AsyncSession = Depends(get_db),
):
    """Send the launch email to every subscriber not yet notified. Re-runnable."""
    result = await db.execute(
        select(WaitlistSubscriber).where(WaitlistSubscriber.notified_at.is_(None))
    )
    pending = list(result.scalars().all())

    skipped_result = await db.execute(
        select(func.count())
        .select_from(WaitlistSubscriber)
        .where(WaitlistSubscriber.notified_at.is_not(None))
    )
    skipped = skipped_result.scalar() or 0

    subject, html = launch_announcement_email()
    sent = 0
    for subscriber in pending:
        if send_email(subscriber.email, subject, html):
            subscriber.notified_at = datetime.now(timezone.utc)
            sent += 1
        else:
            logger.error(f"Launch email failed for {subscriber.email}; left un-notified")
    await db.commit()

    return WaitlistAnnounceResponse(sent=sent, skipped=skipped)
