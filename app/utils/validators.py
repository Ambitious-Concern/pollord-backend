import re
from datetime import datetime, timezone


def validate_password_strength(password: str) -> tuple[bool, str]:
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter"
    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter"
    if not re.search(r"\d", password):
        return False, "Password must contain at least one digit"
    if not re.search(r"[!@#$%^&*()_+\-=\[\]{}|;:',.<>?/`~]", password):
        return False, "Password must contain at least one special character"
    return True, "Password is valid"


def validate_election_dates(start: datetime, end: datetime) -> tuple[bool, str]:
    now = datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    if end <= start:
        return False, "End date must be after start date"
    return True, "Dates are valid"


def validate_ticket_quantity(
    requested: int, available: int, max_per_user: int, existing: int = 0
) -> tuple[bool, str]:
    if requested < 1:
        return False, "Quantity must be at least 1"
    if requested > available:
        return False, f"Only {available} tickets available"
    if existing + requested > max_per_user:
        return False, f"Maximum {max_per_user} tickets per user"
    return True, "Quantity is valid"
