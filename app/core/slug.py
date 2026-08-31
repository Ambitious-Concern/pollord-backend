import re
import secrets


def generate_slug(title: str) -> str:
    """Slugify a title and append a short random suffix so uniqueness doesn't
    need a collision-retry loop against the DB (e.g. "Best Dressed Awards" ->
    "best-dressed-awards-a3f9")."""
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "listing"
    suffix = secrets.token_hex(2)  # 4 hex chars
    return f"{base}-{suffix}"
