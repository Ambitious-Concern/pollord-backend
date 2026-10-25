"""Refuses to seed anything that isn't unambiguously the local dev database.

Both docker-compose.yml (dev) and docker-compose.prod.yml (prod) name the
database service "db", so hostname alone can't tell them apart when the
seeder runs inside a container. Checking against dev's hardcoded
pollard/pollard/pollard credentials (docker-compose.yml) closes that gap —
prod's compose file sources its credentials from env-var secrets instead.
"""
from urllib.parse import urlparse

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_NETWORK_ALIASES = {"db", "postgres"}
_DEV_CREDENTIALS = {"user": "pollard", "password": "pollard", "dbname": "pollard"}
# Known production markers (server IP / domain from .env.example) — refuse
# unconditionally if either shows up anywhere in the URL.
_BLOCKED_MARKERS = ("pollord.com", "62.171.191.76")


class UnsafeSeedTarget(RuntimeError):
    """Raised when DATABASE_URL doesn't look like a local dev database."""


def assert_safe_to_seed(database_url: str) -> None:
    if any(marker in database_url for marker in _BLOCKED_MARKERS):
        raise UnsafeSeedTarget(
            f"DATABASE_URL matches a known production host — refusing to seed.\n{database_url}"
        )

    parsed = urlparse(database_url.replace("postgresql+asyncpg", "postgresql", 1))
    host = parsed.hostname or ""

    if host in _LOCAL_HOSTS:
        return

    if host in _NETWORK_ALIASES:
        creds = {
            "user": parsed.username,
            "password": parsed.password,
            "dbname": (parsed.path or "").lstrip("/"),
        }
        if creds == _DEV_CREDENTIALS:
            return
        raise UnsafeSeedTarget(
            f"Host '{host}' is a docker-network alias shared by dev and prod compose "
            "files, and its credentials don't match the hardcoded dev-only "
            f"pollard/pollard/pollard set in docker-compose.yml — refusing to seed.\n{database_url}"
        )

    raise UnsafeSeedTarget(
        f"Host '{host}' isn't a recognized local/dev database — refusing to seed.\n{database_url}"
    )
