"""The bulk voter pool (for casting realistic vote/ticket-purchase volume)
and the support-agent accounts live chat's queue needs."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.user import User
from app.repositories.user_repository import UserRepository
from seeders import config
from seeders.data.names import unique_full_name


async def seed_voters(db: AsyncSession) -> list[User]:
    """Idempotent by email — re-running the seeder without --reset tops up
    to N_VOTERS rather than duplicating anyone already seeded."""
    result = await db.execute(select(User.email).where(User.email.like("seed+voter%@pollord.dev")))
    existing_emails = {row[0] for row in result.all()}
    if len(existing_emails) >= config.N_VOTERS:
        result = await db.execute(select(User).where(User.email.in_(existing_emails)))
        return list(result.scalars().all())

    user_repo = UserRepository(User, db)
    voters: list[User] = []
    seen_names: set = set()

    result = await db.execute(select(User).where(User.email.in_(existing_emails)))
    voters.extend(result.scalars().all())

    for i in range(len(existing_emails), config.N_VOTERS):
        name = unique_full_name(seen_names)
        email = f"seed+voter{i}@pollord.dev"
        user = User(
            email=email,
            password_hash=hash_password(config.SEED_PASSWORD),
            full_name=name,
            email_verified=True,
            account_status="active",
        )
        db.add(user)
        await db.flush()
        await user_repo.grant_roles_by_name(user.user_id, ["Voter"])
        voters.append(user)

    return voters


async def seed_support_agents(db: AsyncSession) -> list[User]:
    user_repo = UserRepository(User, db)
    agents: list[User] = []
    for i in range(config.N_SUPPORT_AGENTS):
        email = f"seed+agent{i}@pollord.dev"
        result = await db.execute(select(User).where(User.email == email))
        agent = result.scalar_one_or_none()
        if not agent:
            agent = User(
                email=email,
                password_hash=hash_password(config.SEED_PASSWORD),
                full_name=f"Support Agent {i + 1} (seed)",
                email_verified=True,
                account_status="active",
            )
            db.add(agent)
            await db.flush()
            await user_repo.grant_roles_by_name(agent.user_id, ["System Administrator"])
        agents.append(agent)
    return agents
