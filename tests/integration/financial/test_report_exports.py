from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.election import Category, Election
from app.models.event import Event
from app.models.report_export_job import ReportExportJob


async def _make_organizer(db_session: AsyncSession, email: str) -> dict:
    from sqlalchemy import select

    from app.models.user import Role, User, UserRole

    user = User(
        email=email, password_hash=hash_password("Passw0rd!"),
        full_name="Organizer", email_verified=True, account_status="active",
    )
    db_session.add(user)
    await db_session.flush()

    result = await db_session.execute(select(Role).where(Role.role_name == "Event Organizer"))
    role = result.scalar_one_or_none()
    if not role:
        role = Role(role_name="Event Organizer", permissions={})
        db_session.add(role)
        await db_session.flush()
    db_session.add(UserRole(user_id=user.user_id, role_id=role.role_id))
    await db_session.flush()

    token = create_access_token(str(user.user_id))
    return {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


async def _make_election(db_session: AsyncSession, admin_user, *, plan="standard") -> Election:
    now = datetime.now(timezone.utc)
    election = Election(
        title="Export Test Election",
        start_datetime=now - timedelta(days=1),
        end_datetime=now + timedelta(days=1),
        status="active",
        created_by=admin_user["user"].user_id,
        plan=plan,
    )
    db_session.add(election)
    await db_session.flush()
    db_session.add(Category(election_id=election.election_id, name="Overall Queen", election_type="single_choice"))
    await db_session.flush()
    return election


async def _make_event(db_session: AsyncSession, admin_user) -> Event:
    event = Event(
        title="Export Test Event",
        event_date=date(2026, 12, 1),
        event_time=time(18, 0),
        location="Accra",
        status="published",
        created_by=admin_user["user"].user_id,
    )
    db_session.add(event)
    await db_session.flush()
    return event


DELAY_PATCH = "app.services.report_export_service.generate_report_export.delay"


class TestFinancialExportCreation:
    async def test_create_financial_export_returns_pending_job(self, client: AsyncClient, admin_user):
        with patch(DELAY_PATCH) as mock_delay:
            res = await client.post(
                "/api/v1/reports/exports",
                json={"report_type": "financial", "format": "pdf", "params": {"range": "all-time"}},
                headers=admin_user["headers"],
            )
        assert res.status_code == 202, res.text
        body = res.json()
        assert body["status"] == "pending"
        assert body["report_type"] == "financial"
        mock_delay.assert_called_once()

    async def test_duplicate_submit_returns_same_job(self, client: AsyncClient, admin_user):
        payload = {"report_type": "financial", "format": "xlsx", "params": {"range": "last-30"}}
        with patch(DELAY_PATCH) as mock_delay:
            first = await client.post("/api/v1/reports/exports", json=payload, headers=admin_user["headers"])
            second = await client.post("/api/v1/reports/exports", json=payload, headers=admin_user["headers"])
        assert first.json()["job_id"] == second.json()["job_id"]
        # Only enqueued once — the second call reused the existing job.
        mock_delay.assert_called_once()

    async def test_explicit_idempotency_key_overrides_derived_one(self, client: AsyncClient, admin_user):
        with patch(DELAY_PATCH):
            first = await client.post(
                "/api/v1/reports/exports",
                json={"report_type": "financial", "format": "csv", "params": {}, "idempotency_key": "same-key"},
                headers=admin_user["headers"],
            )
            # Different params, but same explicit key -> same job.
            second = await client.post(
                "/api/v1/reports/exports",
                json={"report_type": "financial", "format": "csv", "params": {"range": "last-90"}, "idempotency_key": "same-key"},
                headers=admin_user["headers"],
            )
        assert first.json()["job_id"] == second.json()["job_id"]


class TestVotingExportGating:
    async def test_standard_plan_election_allowed(self, client: AsyncClient, db_session, admin_user):
        election = await _make_election(db_session, admin_user, plan="standard")
        with patch(DELAY_PATCH) as mock_delay:
            res = await client.post(
                "/api/v1/reports/exports",
                json={
                    "report_type": "voting", "format": "pdf",
                    "params": {"source_kind": "election", "source_id": str(election.election_id)},
                },
                headers=admin_user["headers"],
            )
        assert res.status_code == 202, res.text
        mock_delay.assert_called_once()

    async def test_outsider_cannot_export_others_election(self, client: AsyncClient, db_session, admin_user):
        election = await _make_election(db_session, admin_user, plan="premium")
        outsider = await _make_organizer(db_session, "outsider-export@example.com")
        with patch(DELAY_PATCH):
            res = await client.post(
                "/api/v1/reports/exports",
                json={
                    "report_type": "voting", "format": "csv",
                    "params": {"source_kind": "election", "source_id": str(election.election_id)},
                },
                headers=outsider["headers"],
            )
        assert res.status_code == 403

    async def test_missing_params_rejected(self, client: AsyncClient, admin_user):
        res = await client.post(
            "/api/v1/reports/exports",
            json={"report_type": "voting", "format": "pdf", "params": {}},
            headers=admin_user["headers"],
        )
        assert res.status_code == 400


class TestAttendanceExportGating:
    async def test_outsider_cannot_export_others_event(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user)
        outsider = await _make_organizer(db_session, "outsider-attendance@example.com")
        with patch(DELAY_PATCH):
            res = await client.post(
                "/api/v1/reports/exports",
                json={"report_type": "attendance", "format": "xlsx", "params": {"event_id": str(event.event_id)}},
                headers=outsider["headers"],
            )
        assert res.status_code == 403

    async def test_owner_can_request_attendance_export(self, client: AsyncClient, db_session, admin_user):
        event = await _make_event(db_session, admin_user)
        with patch(DELAY_PATCH) as mock_delay:
            res = await client.post(
                "/api/v1/reports/exports",
                json={"report_type": "attendance", "format": "csv", "params": {"event_id": str(event.event_id)}},
                headers=admin_user["headers"],
            )
        assert res.status_code == 202, res.text
        mock_delay.assert_called_once()


class TestGetJob:
    async def test_get_own_job(self, client: AsyncClient, admin_user):
        with patch(DELAY_PATCH):
            create_res = await client.post(
                "/api/v1/reports/exports",
                json={"report_type": "financial", "format": "pdf", "params": {}},
                headers=admin_user["headers"],
            )
        job_id = create_res.json()["job_id"]

        res = await client.get(f"/api/v1/reports/exports/{job_id}", headers=admin_user["headers"])
        assert res.status_code == 200
        assert res.json()["job_id"] == job_id

    async def test_cannot_get_others_job(self, client: AsyncClient, db_session, admin_user):
        with patch(DELAY_PATCH):
            create_res = await client.post(
                "/api/v1/reports/exports",
                json={"report_type": "financial", "format": "pdf", "params": {}},
                headers=admin_user["headers"],
            )
        job_id = create_res.json()["job_id"]

        outsider = await _make_organizer(db_session, "outsider-getjob@example.com")
        res = await client.get(f"/api/v1/reports/exports/{job_id}", headers=outsider["headers"])
        assert res.status_code == 404


class TestRunExportJob:
    async def test_financial_export_runs_end_to_end(self, client: AsyncClient, db_session, admin_user):
        """The full pipeline (assemble -> render -> upload -> mark
        completed), bypassing Celery entirely and calling the async runner
        directly — file_storage_service.upload is mocked since it's a real
        external HTTP call in production."""
        from app.services.report_export_runner import run_export_job

        with patch(DELAY_PATCH):
            create_res = await client.post(
                "/api/v1/reports/exports",
                json={"report_type": "financial", "format": "pdf", "params": {"range": "all-time"}},
                headers=admin_user["headers"],
            )
        job_id = UUID(create_res.json()["job_id"])

        with patch(
            "app.services.report_export_runner.file_storage_service.upload",
            new=AsyncMock(return_value="https://files.example.com/report.pdf"),
        ):
            # db=db_session reuses the test's own transaction — see
            # run_export_job's docstring for why this parameter exists.
            await run_export_job(job_id, db=db_session)

        res = await client.get(f"/api/v1/reports/exports/{job_id}", headers=admin_user["headers"])
        body = res.json()
        assert body["status"] == "completed", body
        assert body["download_url"] == "https://files.example.com/report.pdf"
