from typing import List, Optional
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ownership import require_election_ownership, require_event_ownership
from app.core.plans import meets_minimum_plan
from app.core.security import decode_token
from app.core.session_revocation import is_session_revoked
from app.db.base import get_db
from app.models.election import Election
from app.models.event import Event
from app.models.user import User
from app.repositories.election_repository import ElectionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.user_repository import UserRepository

bearer_scheme = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """FastAPI dependency: resolves the bearer token to a User, or raises
    401. Any authenticated endpoint depends on this (directly or via
    get_current_active_user / require_roles)."""
    token = credentials.credentials
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    payload = decode_token(token)
    if payload is None:
        raise credentials_exception

    token_type = payload.get("type")
    if token_type != "access":
        raise credentials_exception

    user_id = payload.get("sub")
    if user_id is None:
        raise credentials_exception

    # Checked before touching the DB — a single revoked device should be
    # rejected immediately, not just once its refresh eventually fails (see
    # revoke_session in users.py, which is what actually writes this key).
    session_id = payload.get("sid")
    if session_id and await is_session_revoked(session_id):
        raise credentials_exception

    user_repo = UserRepository(User, db)
    user = await user_repo.get_with_roles(UUID(user_id))
    if user is None:
        raise credentials_exception

    # Tokens issued before a logout/password change carry a stale "ver"
    # claim and must be rejected even though the signature is still valid.
    if payload.get("ver", 0) != user.token_version:
        raise credentials_exception

    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Same as get_current_user, plus rejects suspended/deactivated accounts."""
    if current_user.account_status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is not active",
        )
    return current_user


async def get_current_session_id(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> Optional[str]:
    """The `sid` claim on the presented access token — the paired refresh
    token's own jti (see create_access_token). Lets an endpoint identify
    which row in `refresh_tokens` the caller is currently using, e.g. to
    mark "this device" in the Security settings page's session list."""
    payload = decode_token(credentials.credentials)
    return payload.get("sid") if payload else None


# Roles that may READ the admin console. System Administrator can also write;
# Platform Viewer cannot, so it is deliberately absent from every write guard.
# Enforced here rather than by hiding buttons — a viewer can call the API.
CONSOLE_READ_ROLES = ("System Administrator", "Platform Viewer")


def require_roles(*roles: str):
    """Dependency factory: require_roles("Admin", "Organizer") builds a
    dependency that 403s unless the user has at least one of those roles."""

    async def role_checker(
        current_user: User = Depends(get_current_active_user),
    ) -> User:
        user_role_names = [ur.role.role_name for ur in current_user.user_roles]
        if not any(r in user_role_names for r in roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return current_user

    return role_checker


def require_election_plan(minimum: str):
    """404s if missing, 403s if not owned or below `minimum` plan."""

    async def checker(
        request: Request,
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_active_user),
    ) -> Election:
        election_id = UUID(request.path_params["election_id"])
        election = await ElectionRepository(Election, db).get_by_id(
            election_id, id_field="election_id"
        )
        if not election:
            raise HTTPException(status_code=404, detail="Election not found")
        await require_election_ownership(election, current_user, db)
        if not meets_minimum_plan(election.plan, minimum):
            raise HTTPException(
                status_code=403,
                detail=f"This feature requires the {minimum} plan or higher",
            )
        return election

    return checker


def require_event_plan(minimum: str):
    """404s if missing, 403s if not owned or below `minimum` plan."""

    async def checker(
        request: Request,
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_active_user),
    ) -> Event:
        event_id = UUID(request.path_params["event_id"])
        event = await EventRepository(Event, db).get_by_id(event_id, id_field="event_id")
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        await require_event_ownership(event, current_user, db)
        if not meets_minimum_plan(event.plan, minimum):
            raise HTTPException(
                status_code=403,
                detail=f"This feature requires the {minimum} plan or higher",
            )
        return event

    return checker
