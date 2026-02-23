import logging
from typing import Optional

logger = logging.getLogger(__name__)


class NotificationService:
    async def send_verification_email(self, email: str, token: str) -> None:
        # TODO: Integrate with Celery email task
        logger.info(f"Verification email queued for {email}")

    async def send_password_reset_email(self, email: str, token: str) -> None:
        logger.info(f"Password reset email queued for {email}")

    async def send_ticket_confirmation(
        self, email: str, event_title: str, ticket_count: int
    ) -> None:
        logger.info(
            f"Ticket confirmation email queued for {email}: "
            f"{ticket_count} tickets for {event_title}"
        )

    async def send_vote_confirmation(
        self, email: str, election_title: str, receipt_code: str
    ) -> None:
        logger.info(
            f"Vote confirmation email queued for {email}: "
            f"Election '{election_title}', receipt {receipt_code}"
        )

    async def send_election_notification(
        self, email: str, election_title: str
    ) -> None:
        logger.info(f"Election notification email queued for {email}: {election_title}")

    async def send_event_cancellation(
        self, email: str, event_title: str
    ) -> None:
        logger.info(f"Event cancellation email queued for {email}: {event_title}")
