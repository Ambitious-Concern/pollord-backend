import logging

from app.services import email_service

logger = logging.getLogger(__name__)


class NotificationService:
    async def send_verification_email(self, email: str, token: str) -> None:
        from app.core.config import settings
        link = f"{settings.FRONTEND_URL}/verify-email?token={token}"
        subject, html = email_service.password_reset_email("User", link)
        subject = "Verify Your Email: Pollord"
        email_service.send_email(email, subject, html)

    async def send_password_reset_email(self, email: str, token: str) -> None:
        from app.core.config import settings
        link = f"{settings.FRONTEND_URL}/reset-password?token={token}"
        subject, html = email_service.password_reset_email("User", link)
        email_service.send_email(email, subject, html)

    async def send_ticket_confirmation(
        self, email: str, event_title: str, ticket_count: int
    ) -> None:
        from app.core.config import settings
        subject = f"Ticket Confirmation: {event_title}"
        content = f"""
        <h2>Ticket Confirmation</h2>
        <p>You have successfully purchased <span class="highlight">{ticket_count} ticket(s)</span> for:</p>
        <div class="otp-box">
          <div style="color:#fff; font-size:20px; font-weight:800;">{event_title}</div>
        </div>
        <p>Your tickets are available in <a href="{settings.FRONTEND_URL}/tickets/my-tickets" style="color:#6C63FF;">My Tickets</a>.</p>
        """
        html = email_service._base_template(content, f"{ticket_count} ticket(s) confirmed for {event_title}")
        email_service.send_email(email, subject, html)

    async def send_vote_confirmation(
        self, email: str, election_title: str, receipt_code: str
    ) -> None:
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
        html = email_service._base_template(content, f"Vote confirmed in {election_title}")
        email_service.send_email(email, subject, html)

    async def send_election_notification(
        self, email: str, election_title: str, election_id: str = ""
    ) -> None:
        subject, html = email_service.election_invite_email("Voter", election_title, election_id)
        email_service.send_email(email, subject, html)

    async def send_event_cancellation(
        self, email: str, event_title: str
    ) -> None:
        subject = f"Event Cancelled: {event_title}"
        content = f"""
        <h2>Event Cancelled</h2>
        <p>We're sorry to inform you that the following event has been cancelled:</p>
        <div class="otp-box">
          <div style="color:#fff; font-size:20px; font-weight:800;">{event_title}</div>
        </div>
        <p>If you purchased tickets, you will be refunded. Please contact support if you have questions.</p>
        """
        html = email_service._base_template(content, f"{event_title} has been cancelled")
        email_service.send_email(email, subject, html)
