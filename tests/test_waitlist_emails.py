from app.services.email_service import (
    launch_announcement_email,
    waitlist_confirmation_email,
)


def test_waitlist_confirmation_email_mentions_launch_date():
    subject, html = waitlist_confirmation_email()
    assert "list" in subject.lower()
    assert "August 13, 2026" in html
    assert "Pollord" in html


def test_launch_announcement_email_links_to_frontend():
    from app.core.config import settings

    subject, html = launch_announcement_email()
    assert "live" in subject.lower()
    assert settings.FRONTEND_URL in html
