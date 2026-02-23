import logging

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_email_task(self, to: str, subject: str, body: str):
    try:
        # TODO: Integrate with FastAPI-Mail or SMTP
        logger.info(f"Sending email to {to}: {subject}")
    except Exception as exc:
        logger.error(f"Failed to send email to {to}: {exc}")
        raise self.retry(exc=exc)


@celery_app.task
def send_verification_email(email: str, token: str):
    subject = "Verify Your Email - Pollard Platform"
    body = f"Click the link to verify your email: /verify?token={token}"
    send_email_task.delay(email, subject, body)


@celery_app.task
def send_password_reset_email(email: str, token: str):
    subject = "Reset Your Password - Pollard Platform"
    body = f"Click the link to reset your password: /reset-password?token={token}"
    send_email_task.delay(email, subject, body)


@celery_app.task
def send_ticket_confirmation_email(
    email: str, event_title: str, ticket_count: int
):
    subject = f"Ticket Confirmation - {event_title}"
    body = f"You have purchased {ticket_count} ticket(s) for {event_title}."
    send_email_task.delay(email, subject, body)


@celery_app.task
def send_vote_confirmation_email(
    email: str, election_title: str, receipt_code: str
):
    subject = f"Vote Confirmation - {election_title}"
    body = f"Your vote has been recorded. Receipt code: {receipt_code}"
    send_email_task.delay(email, subject, body)
