import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


def _base_template(content: str, preview_text: str = "") -> str:
    """Wrap content in the Pollord email template matching the frontend design."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pollord</title>
<style>
  body {{ margin:0; padding:0; background-color:#1a1a2e; font-family: 'Montserrat', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
  .wrapper {{ max-width:600px; margin:0 auto; }}
  .header {{ background: linear-gradient(135deg, #6C63FF 0%, #7c3aed 100%); padding:40px 32px 32px; text-align:center; border-radius:0 0 24px 24px; }}
  .header h1 {{ color:#fff; font-size:22px; font-weight:900; letter-spacing:0.1em; text-transform:uppercase; margin:0; }}
  .header .tagline {{ color:rgba(255,255,255,0.7); font-size:12px; letter-spacing:0.2em; text-transform:uppercase; margin-top:8px; }}
  .body {{ background-color:#1e1e3a; padding:40px 32px; }}
  .body h2 {{ color:#fff; font-size:24px; font-weight:800; margin:0 0 16px; }}
  .body p {{ color:rgba(255,255,255,0.7); font-size:15px; line-height:1.7; margin:0 0 16px; }}
  .otp-box {{ background:rgba(108,99,255,0.12); border:2px solid rgba(108,99,255,0.3); border-radius:16px; padding:24px; text-align:center; margin:24px 0; }}
  .otp-code {{ color:#6C63FF; font-size:36px; font-weight:900; letter-spacing:0.3em; font-family:monospace; }}
  .otp-label {{ color:rgba(255,255,255,0.5); font-size:12px; text-transform:uppercase; letter-spacing:0.15em; margin-top:8px; }}
  .btn {{ display:inline-block; background:#6C63FF; color:#fff; text-decoration:none; padding:14px 32px; border-radius:12px; font-weight:700; font-size:14px; text-transform:uppercase; letter-spacing:0.1em; }}
  .btn:hover {{ background:#5a52d9; }}
  .divider {{ border:none; border-top:1px solid rgba(255,255,255,0.06); margin:24px 0; }}
  .footer {{ padding:32px; text-align:center; }}
  .footer p {{ color:rgba(255,255,255,0.3); font-size:12px; line-height:1.6; margin:0; }}
  .footer a {{ color:rgba(108,99,255,0.7); text-decoration:none; }}
  .highlight {{ color:#6C63FF; font-weight:700; }}
  .preview {{ display:none; max-height:0; overflow:hidden; mso-hide:all; }}
</style>
</head>
<body>
<div class="preview">{preview_text}</div>
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#1a1a2e; padding:24px 16px;">
<tr><td>
<div class="wrapper">
  <div class="header">
    <h1>Pollord</h1>
    <div class="tagline">Elections &amp; Events Platform</div>
  </div>
  <div class="body">
    {content}
  </div>
  <div class="footer">
    <hr class="divider">
    <p>
      &copy; 2026 Pollord. All rights reserved.<br>
      <a href="{settings.FRONTEND_URL}">Visit Pollord</a>
    </p>
  </div>
</div>
</td></tr>
</table>
</body>
</html>"""


def welcome_email(user_name: str) -> tuple[str, str]:
    """Return (subject, html_body) for the welcome email."""
    subject = "Welcome to Pollord!"
    content = f"""
    <h2>Welcome, {user_name}! 🎉</h2>
    <p>Your account has been created successfully. You now have full access to create elections, manage events, and sell tickets.</p>
    <hr class="divider">
    <p><strong>What you can do:</strong></p>
    <p>✅ Create and manage elections with encrypted voting<br>
       ✅ Organize events with digital ticketing<br>
       ✅ Track real-time results and analytics<br>
       ✅ Invite team members to collaborate</p>
    <hr class="divider">
    <p>Complete your KYC to set up your organization and unlock all features.</p>
    <p style="text-align:center; margin-top:24px;">
      <a href="{settings.FRONTEND_URL}/dashboard" class="btn">Go to Dashboard</a>
    </p>
    """
    return subject, _base_template(content, f"Welcome to Pollord, {user_name}!")


def otp_email(user_name: str, otp_code: str) -> tuple[str, str]:
    """Return (subject, html_body) for OTP verification."""
    subject = f"Your Pollord verification code: {otp_code}"
    content = f"""
    <h2>Verify Your Email</h2>
    <p>Hi {user_name}, use the code below to verify your email address.</p>
    <div class="otp-box">
      <div class="otp-code">{otp_code}</div>
      <div class="otp-label">Verification Code</div>
    </div>
    <p>This code expires in <span class="highlight">{settings.OTP_EXPIRE_MINUTES} minutes</span>. If you didn't request this, you can safely ignore this email.</p>
    """
    return subject, _base_template(content, f"Your code is {otp_code}")


def password_reset_email(user_name: str, reset_link: str) -> tuple[str, str]:
    """Return (subject, html_body) for password reset."""
    subject = "Reset Your Password — Pollord"
    content = f"""
    <h2>Reset Your Password</h2>
    <p>Hi {user_name}, we received a request to reset your password.</p>
    <p style="text-align:center; margin:32px 0;">
      <a href="{reset_link}" class="btn">Reset Password</a>
    </p>
    <p>This link expires in 1 hour. If you didn't request this, ignore this email.</p>
    """
    return subject, _base_template(content, "Reset your Pollord password")


def election_invite_email(
    user_name: str, election_title: str, election_id: str
) -> tuple[str, str]:
    """Return (subject, html_body) for election voter invite."""
    subject = f"You're invited to vote — {election_title}"
    link = f"{settings.FRONTEND_URL}/voting/ballot/{election_id}"
    content = f"""
    <h2>You're Invited to Vote</h2>
    <p>Hi {user_name}, you've been added as an eligible voter for:</p>
    <div class="otp-box">
      <div style="color:#fff; font-size:20px; font-weight:800;">{election_title}</div>
    </div>
    <p style="text-align:center; margin-top:24px;">
      <a href="{link}" class="btn">Cast Your Vote</a>
    </p>
    """
    return subject, _base_template(content, f"Vote now in {election_title}")


def candidate_nomination_email(
    candidate_name: str,
    election_title: str,
    election_id: str,
    candidate_email: str,
    start_datetime: str,
    end_datetime: str,
    org_name: str,
) -> tuple[str, str]:
    """Return (subject, html_body) notifying someone they've been added as a candidate."""
    subject = f"You've been nominated — {election_title}"
    import urllib.parse
    results_url = (
        f"{settings.FRONTEND_URL}/candidate/results"
        f"?election_id={election_id}&email={urllib.parse.quote(candidate_email)}"
    )
    content = f"""
    <h2>You've Been Nominated</h2>
    <p>Hi <span class="highlight">{candidate_name}</span>,</p>
    <p>You have been added as a candidate in the following election organized by <span class="highlight">{org_name}</span>:</p>
    <div class="otp-box">
      <div style="color:#fff; font-size:20px; font-weight:800;">{election_title}</div>
      <div style="color:rgba(255,255,255,0.6); font-size:13px; margin-top:8px;">
        Voting period: {start_datetime} &ndash; {end_datetime}
      </div>
    </div>
    <p>During and after the election you can track your personal performance in real time. Click the button below — you'll be asked to verify your email with a one-time code.</p>
    <p style="text-align:center; margin:24px 0;">
      <a href="{results_url}" class="btn">View My Results</a>
    </p>
    <hr class="divider">
    <p style="font-size:13px; color:rgba(255,255,255,0.4);">
      If you believe this was sent in error, please ignore this email or contact the election organizer.
    </p>
    """
    return subject, _base_template(content, f"You've been nominated for {election_title}")


def candidate_otp_email(candidate_name: str, otp_code: str, election_title: str) -> tuple[str, str]:
    """OTP email for candidate result access (no account required)."""
    subject = f"Your results access code — {election_title}"
    content = f"""
    <h2>Your Access Code</h2>
    <p>Hi {candidate_name}, use the code below to view your results for <span class="highlight">{election_title}</span>.</p>
    <div class="otp-box">
      <div class="otp-code">{otp_code}</div>
      <div class="otp-label">One-Time Access Code</div>
    </div>
    <p>This code expires in <span class="highlight">10 minutes</span>. If you didn't request this, you can safely ignore it.</p>
    """
    return subject, _base_template(content, f"Your access code: {otp_code}")


def candidate_result_link_email(
    candidate_name: str,
    election_title: str,
    results_url: str,
) -> tuple[str, str]:
    """Persistent link email sent after successful OTP verification."""
    subject = f"Your results link — {election_title}"
    content = f"""
    <h2>Bookmark Your Results</h2>
    <p>Hi {candidate_name}, here is your personal link to view your performance in <span class="highlight">{election_title}</span> at any time.</p>
    <p style="text-align:center; margin:32px 0;">
      <a href="{results_url}" class="btn">View My Results</a>
    </p>
    <p style="font-size:13px; color:rgba(255,255,255,0.5);">
      This link is unique to you and expires 2 weeks after the election ends. Keep it safe.
    </p>
    """
    return subject, _base_template(content, f"Your results for {election_title}")


def org_invitation_email(
    org_name: str,
    inviter_name: str,
    role: str,
    accept_url: str,
) -> tuple[str, str]:
    """Return (subject, html_body) for an organization invitation."""
    subject = f"You've been invited to join {org_name} on Pollord"
    role_label = role.capitalize()
    content = f"""
    <h2>You're Invited to Join {org_name}</h2>
    <p><span class="highlight">{inviter_name}</span> has invited you to join <span class="highlight">{org_name}</span> as a <span class="highlight">{role_label}</span>.</p>
    <p>Click the button below to accept the invitation. You'll be asked to create an account if you don't have one yet.</p>
    <p style="text-align:center; margin:32px 0;">
      <a href="{accept_url}" class="btn">Accept Invitation</a>
    </p>
    <hr class="divider">
    <p style="font-size:13px; color:rgba(255,255,255,0.4);">
      This invitation expires in 7 days. If you weren't expecting this, you can safely ignore it.
    </p>
    """
    return subject, _base_template(content, f"Join {org_name} on Pollord")


def send_email(to: str, subject: str, html_body: str) -> bool:
    """Send an email via Zoho SMTP with SSL."""
    if not settings.MAIL_PASSWORD:
        logger.warning(f"MAIL_PASSWORD not set — skipping email to {to}")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{settings.MAIL_FROM_NAME} <{settings.MAIL_FROM}>"
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))

        context = ssl.create_default_context()

        if settings.MAIL_USE_SSL:
            with smtplib.SMTP_SSL(settings.MAIL_HOST, settings.MAIL_PORT, context=context) as server:
                server.login(settings.MAIL_USERNAME, settings.MAIL_PASSWORD)
                server.sendmail(settings.MAIL_FROM, to, msg.as_string())
        else:
            with smtplib.SMTP(settings.MAIL_HOST, settings.MAIL_PORT) as server:
                server.starttls(context=context)
                server.login(settings.MAIL_USERNAME, settings.MAIL_PASSWORD)
                server.sendmail(settings.MAIL_FROM, to, msg.as_string())

        logger.info(f"Email sent to {to}: {subject}")
        return True

    except Exception as e:
        logger.error(f"Failed to send email to {to}: {e}")
        return False
