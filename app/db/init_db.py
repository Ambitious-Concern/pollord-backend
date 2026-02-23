import logging

from sqlalchemy import select

from app.core.security import hash_password
from app.db.base import async_session_maker
from app.models.user import Role, User, UserRole

logger = logging.getLogger(__name__)

DEFAULT_ROLES = [
    {
        "role_name": "System Administrator",
        "permissions": {
            "users": ["create", "read", "update", "delete"],
            "elections": ["create", "read", "update", "delete"],
            "events": ["create", "read", "update", "delete"],
            "tickets": ["create", "read", "update", "delete"],
            "analytics": ["read"],
            "audit_logs": ["read"],
            "admin": ["full"],
        },
    },
    {
        "role_name": "Election Administrator",
        "permissions": {
            "elections": ["create", "read", "update", "delete"],
            "analytics": ["read"],
        },
    },
    {
        "role_name": "Event Organizer",
        "permissions": {
            "events": ["create", "read", "update", "delete"],
            "tickets": ["create", "read", "update"],
            "analytics": ["read"],
        },
    },
    {
        "role_name": "Voter",
        "permissions": {
            "elections": ["read"],
            "voting": ["cast", "read"],
        },
    },
    {
        "role_name": "Event Attendee",
        "permissions": {
            "events": ["read"],
            "tickets": ["read", "purchase"],
        },
    },
]


async def init_db() -> None:
    async with async_session_maker() as session:
        for role_data in DEFAULT_ROLES:
            result = await session.execute(
                select(Role).where(Role.role_name == role_data["role_name"])
            )
            existing = result.scalar_one_or_none()
            if existing is None:
                role = Role(
                    role_name=role_data["role_name"],
                    permissions=role_data["permissions"],
                )
                session.add(role)
                logger.info(f"Created role: {role_data['role_name']}")
        await session.commit()

        # Seed default admin user
        result = await session.execute(
            select(User).where(User.email == "admin@pollard.com")
        )
        admin_user = result.scalar_one_or_none()
        if admin_user is None:
            admin_user = User(
                email="admin@pollard.com",
                password_hash=hash_password("Admin@1234"),
                full_name="System Admin",
                email_verified=True,
                account_status="active",
            )
            session.add(admin_user)
            await session.flush()

            result = await session.execute(
                select(Role).where(Role.role_name == "System Administrator")
            )
            admin_role = result.scalar_one()
            session.add(UserRole(user_id=admin_user.user_id, role_id=admin_role.role_id))
            await session.commit()
            logger.info("Created default admin user: admin@pollard.com")

        logger.info("Database initialization complete")
