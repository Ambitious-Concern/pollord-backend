import pytest
from sqlalchemy import select
from app.models.user import Role

@pytest.mark.asyncio
async def test_roles_seeded(db_session):
    result = await db_session.execute(select(Role))
    roles = result.scalars().all()
    print("ROLES FOUND:", [r.role_name for r in roles])
    assert len(roles) > 0
