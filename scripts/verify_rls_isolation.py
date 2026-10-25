"""
Standalone proof that org-level row-level security actually isolates data —
run this against a dev/staging DB that has had `alembic upgrade head` applied
(NOT the pytest test DB, which never runs migrations — see conftest.py).

This connects directly as pollord_app_rls (the restricted role Migration C
creates), never touching settings.DATABASE_URL/async_session_maker for the
assertions themselves — only for bootstrapping and cleaning up the throwaway
data, since pollord_app_rls can't see rows with no org context set yet.

Exercises one representative table per predicate shape (root org_id,
dual-parent election_id/event_id, category_id 2-hop) rather than all 13
phase-1 tables — every table within a shape uses the identical SQL helper
function, so this is proof of the mechanism, not just one table's policy.

Usage:
    docker compose exec app python scripts/verify_rls_isolation.py
"""
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.db.base import async_session_maker
from app.models.election import Candidate, Category, Election
from app.models.organization import Organization
from app.models.user import User

FAILURES: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "ok" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        FAILURES.append(label)


def rls_engine_url() -> str:
    """settings.DATABASE_URL with the credentials swapped for pollord_app_rls
    — never the app's own unrestricted role."""
    if not settings.RLS_APP_ROLE_PASSWORD:
        raise RuntimeError("RLS_APP_ROLE_PASSWORD is not set — cannot connect as pollord_app_rls")
    url = str(settings.DATABASE_URL)
    scheme, rest = url.split("://", 1)
    _, host_part = rest.split("@", 1)
    return f"{scheme}://pollord_app_rls:{settings.RLS_APP_ROLE_PASSWORD}@{host_part}"


async def bootstrap():
    """Two throwaway orgs, one election (+category+candidate) each, created
    via the ORM (so every column's real default is applied exactly as it
    would be through the actual API, rather than this script silently
    drifting from the schema by hand-rolling INSERTs) using the unrestricted
    role — pollord_app_rls can't insert anything without an org context
    already set to satisfy WITH CHECK."""
    ids = {
        "org_a": uuid.uuid4(), "org_b": uuid.uuid4(),
        "election_a": uuid.uuid4(), "election_b": uuid.uuid4(),
        "category_a": uuid.uuid4(), "category_b": uuid.uuid4(),
        "candidate_a": uuid.uuid4(), "candidate_b": uuid.uuid4(),
    }
    from datetime import datetime, timedelta, timezone

    async with async_session_maker() as db:
        users = {}
        for suffix in ("a", "b"):
            user = User(
                user_id=uuid.uuid4(),
                email=f"rls-verify-{suffix}-{uuid.uuid4()}@example.invalid",
                password_hash="x",
                full_name="RLS verify",
                account_status="active",
            )
            db.add(user)
            users[suffix] = user
        await db.flush()

        now = datetime.now(timezone.utc)
        for suffix in ("a", "b"):
            db.add(Organization(
                org_id=ids[f"org_{suffix}"], name="RLS verify", owner_id=users[suffix].user_id,
            ))
            db.add(Election(
                election_id=ids[f"election_{suffix}"], title="RLS verify",
                start_datetime=now, end_datetime=now + timedelta(days=1),
                created_by=users[suffix].user_id, org_id=ids[f"org_{suffix}"],
            ))
        await db.flush()

        for suffix in ("a", "b"):
            db.add(Category(
                category_id=ids[f"category_{suffix}"], election_id=ids[f"election_{suffix}"],
                name="RLS verify", election_type="single_choice",
            ))
        await db.flush()

        for suffix in ("a", "b"):
            db.add(Candidate(
                candidate_id=ids[f"candidate_{suffix}"], category_id=ids[f"category_{suffix}"],
                name="RLS verify",
            ))
        await db.commit()

        ids["user_a"], ids["user_b"] = users["a"].user_id, users["b"].user_id
    return ids


async def cleanup(ids: dict):
    async with async_session_maker() as db:
        await db.execute(delete(Candidate).where(Candidate.candidate_id.in_(
            [ids["candidate_a"], ids["candidate_b"]]
        )))
        await db.execute(delete(Category).where(Category.category_id.in_(
            [ids["category_a"], ids["category_b"]]
        )))
        await db.execute(delete(Election).where(Election.election_id.in_(
            [ids["election_a"], ids["election_b"]]
        )))
        await db.execute(delete(Organization).where(Organization.org_id.in_(
            [ids["org_a"], ids["org_b"]]
        )))
        await db.execute(delete(User).where(User.user_id.in_(
            [ids["user_a"], ids["user_b"]]
        )))
        await db.commit()


async def set_org_context(conn, org_ids_csv: str):
    await conn.execute(text("SELECT set_config('app.org_ids', :v, false)"), {"v": org_ids_csv})


async def run_assertions(ids: dict):
    rls_engine = create_async_engine(rls_engine_url())
    try:
        async with rls_engine.connect() as conn:
            # --- own org visible, other org invisible, across every shape ---
            await set_org_context(conn, str(ids["org_a"]))

            visible_election = (await conn.execute(
                text("SELECT 1 FROM elections WHERE election_id = :id"), {"id": ids["election_a"]}
            )).first()
            check("own org's election is visible", visible_election is not None)

            hidden_election = (await conn.execute(
                text("SELECT 1 FROM elections WHERE election_id = :id"), {"id": ids["election_b"]}
            )).first()
            check("other org's election is invisible", hidden_election is None)

            visible_category = (await conn.execute(
                text("SELECT 1 FROM categories WHERE category_id = :id"), {"id": ids["category_a"]}
            )).first()
            check("own org's category is visible (dual-parent join)", visible_category is not None)

            hidden_category = (await conn.execute(
                text("SELECT 1 FROM categories WHERE category_id = :id"), {"id": ids["category_b"]}
            )).first()
            check("other org's category is invisible (dual-parent join)", hidden_category is None)

            visible_candidate = (await conn.execute(
                text("SELECT 1 FROM candidates WHERE candidate_id = :id"), {"id": ids["candidate_a"]}
            )).first()
            check("own org's candidate is visible (category_id 2-hop join)", visible_candidate is not None)

            hidden_candidate = (await conn.execute(
                text("SELECT 1 FROM candidates WHERE candidate_id = :id"), {"id": ids["candidate_b"]}
            )).first()
            check("other org's candidate is invisible (category_id 2-hop join)", hidden_candidate is None)

            # --- cross-org write rejected by WITH CHECK ---
            try:
                await conn.execute(
                    text(
                        "INSERT INTO categories (category_id, election_id, name, election_type) "
                        "VALUES (:id, :election, 'should be rejected', 'single_choice')"
                    ),
                    {"id": uuid.uuid4(), "election": ids["election_b"]},
                )
                await conn.rollback()
                check("insert into other org's election is rejected", False)
            except Exception:
                await conn.rollback()
                check("insert into other org's election is rejected", True)

            # --- fail-closed: no context set at all ---
            await set_org_context(conn, "")
            no_context_election = (await conn.execute(
                text("SELECT 1 FROM elections WHERE election_id IN (:a, :b)"),
                {"a": ids["election_a"], "b": ids["election_b"]},
            )).first()
            check("no org context -> zero rows visible (fail-closed)", no_context_election is None)
    finally:
        await rls_engine.dispose()


async def main():
    print("Bootstrapping throwaway orgs/elections via the unrestricted role...")
    ids = await bootstrap()
    try:
        print("Running isolation assertions as pollord_app_rls...")
        await run_assertions(ids)
    finally:
        print("Cleaning up throwaway data...")
        await cleanup(ids)

    if FAILURES:
        print(f"\n{len(FAILURES)} assertion(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("\nAll assertions passed.")


if __name__ == "__main__":
    asyncio.run(main())
