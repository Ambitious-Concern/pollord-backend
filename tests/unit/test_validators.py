from datetime import datetime, timedelta, timezone

from app.utils.validators import (
    validate_election_dates,
    validate_password_strength,
    validate_ticket_quantity,
)


class TestPasswordStrength:
    def test_valid_password(self):
        valid, msg = validate_password_strength("Test1234!")
        assert valid

    def test_too_short(self):
        valid, msg = validate_password_strength("Te1!")
        assert not valid
        assert "8 characters" in msg

    def test_no_uppercase(self):
        valid, msg = validate_password_strength("test1234!")
        assert not valid
        assert "uppercase" in msg

    def test_no_lowercase(self):
        valid, msg = validate_password_strength("TEST1234!")
        assert not valid
        assert "lowercase" in msg

    def test_no_digit(self):
        valid, msg = validate_password_strength("TestTest!")
        assert not valid
        assert "digit" in msg

    def test_no_special(self):
        valid, msg = validate_password_strength("Test12345")
        assert not valid
        assert "special" in msg


class TestElectionDates:
    def test_valid_dates(self):
        start = datetime.now(timezone.utc) + timedelta(days=1)
        end = start + timedelta(days=7)
        valid, msg = validate_election_dates(start, end)
        assert valid

    def test_end_before_start(self):
        start = datetime.now(timezone.utc) + timedelta(days=7)
        end = datetime.now(timezone.utc) + timedelta(days=1)
        valid, msg = validate_election_dates(start, end)
        assert not valid


class TestTicketQuantity:
    def test_valid_quantity(self):
        valid, msg = validate_ticket_quantity(2, 10, 5)
        assert valid

    def test_zero_quantity(self):
        valid, msg = validate_ticket_quantity(0, 10, 5)
        assert not valid

    def test_exceeds_available(self):
        valid, msg = validate_ticket_quantity(11, 10, 15)
        assert not valid

    def test_exceeds_per_user_limit(self):
        valid, msg = validate_ticket_quantity(3, 10, 5, existing=4)
        assert not valid
        assert "Maximum" in msg
