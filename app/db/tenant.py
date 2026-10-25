"""Tenant-scoped DB session — sets app.org_ids for RLS policies to read.
No-op until the app's DB role is switched off superuser (see the RLS plan)."""
from typing import AsyncGenerator

from fastapi import Depends
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_active_user
from app.core.tenant_sql import render_org_ids_literal
from app.db.base import get_db
from app.models.organization import Organization
from app.models.user import User
from app.repositories.organization_repository import OrganizationRepository


async def get_tenant_db(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> AsyncGenerator[AsyncSession, None]:
    org_ids = await OrganizationRepository(Organization, db).get_org_ids_for_user(
        current_user.user_id
    )
    org_ids_csv = render_org_ids_literal(org_ids)

    # Re-set on every transaction, not just once — some services commit
    # mid-request, which ends the SET LOCAL scope.
    @event.listens_for(db.sync_session, "after_begin")
    def _set_tenant_context(session, transaction, connection):
        connection.execute(
            text("SELECT set_config('app.org_ids', :org_ids, true)"),
            {"org_ids": org_ids_csv},
        )

    yield db
