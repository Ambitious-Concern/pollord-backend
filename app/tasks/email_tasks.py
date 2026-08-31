import logging

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_email_task(self, to: str, subject: str, html_body: str):
    try:
        from app.services.email_service import send_email
        success = send_email(to, subject, html_body)
        if not success:
            raise RuntimeError(f"send_email returned False for {to}")
        logger.info(f"Email sent via Celery to {to}: {subject}")
    except Exception as exc:
        logger.error(f"Failed to send email to {to}: {exc}")
        raise self.retry(exc=exc)


@celery_app.task
def send_verification_email(email: str, token: str):
    from app.core.config import settings
    from app.services.email_service import password_reset_email
    link = f"{settings.FRONTEND_URL}/verify-email?token={token}"
    subject, html = password_reset_email("User", link)
    subject = "Verify Your Email: Pollord"
    send_email_task.delay(email, subject, html)


@celery_app.task
def send_password_reset_email(email: str, token: str):
    from app.core.config import settings
    from app.services.email_service import password_reset_email
    link = f"{settings.FRONTEND_URL}/reset-password?token={token}"
    subject, html = password_reset_email("User", link)
    send_email_task.delay(email, subject, html)


@celery_app.task
def send_ticket_confirmation_email(email: str, event_title: str, ticket_count: int):
    from app.core.config import settings
    from app.services.email_service import _base_template
    subject = f"Ticket Confirmation: {event_title}"
    content = f"""
    <h2>Ticket Confirmation</h2>
    <p>You have successfully purchased <span class="highlight">{ticket_count} ticket(s)</span> for:</p>
    <div class="otp-box">
      <div style="color:#fff; font-size:20px; font-weight:800;">{event_title}</div>
    </div>
    <p>Your tickets are available in <a href="{settings.FRONTEND_URL}/tickets/my-tickets" style="color:#6C63FF;">My Tickets</a>.</p>
    """
    html = _base_template(content, f"{ticket_count} ticket(s) confirmed for {event_title}")
    send_email_task.delay(email, subject, html)


@celery_app.task
def send_vote_confirmation_email(email: str, election_title: str, receipt_code: str):
    from app.services.email_service import _base_template
    subject = f"Vote Confirmation: {election_title}"
    content = f"""
    <h2>Your Vote Has Been Recorded</h2>
    <p>Thank you for voting in:</p>
    <div class="otp-box">
      <div style="color:#fff; font-size:20px; font-weight:800;">{election_title}</div>
    </div>
    <p>Your vote receipt code:</p>
    <div class="otp-box">
      <div class="otp-code" style="font-size:24px;">{receipt_code}</div>
      <div class="otp-label">Keep this safe: it proves you voted</div>
    </div>
    """
    html = _base_template(content, f"Vote confirmed in {election_title}")
    send_email_task.delay(email, subject, html)
