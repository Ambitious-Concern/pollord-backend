from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.election import Category, Election


async def _make_election_with_category(db_session: AsyncSession, admin_user) -> tuple[Election, Category]:
    now = datetime.now(timezone.utc)
    election = Election(
        title="Nominations Test Election",
        start_datetime=now + timedelta(days=1),
        end_datetime=now + timedelta(days=2),
        status="draft",
        created_by=admin_user["user"].user_id,
    )
    db_session.add(election)
    await db_session.flush()
    category = Category(
        election_id=election.election_id, name="Best Dressed",
        election_type="single_choice", display_order=0,
    )
    db_session.add(category)
    await db_session.flush()
    return election, category


FORM_PAYLOAD = {
    "title": "2026 Nominee Form",
    "description": "Nominate anyone",
    "visibility": "public",
    "fields": [
        {"field_type": "paragraph", "label": "Why?", "required": False},
        {"field_type": "short-text", "label": "Contact", "required": True},
    ],
}


class TestNominationFormBuilder:
    async def test_create_form_persists_fields_in_order(self, client: AsyncClient, db_session, admin_user):
        election, _category = await _make_election_with_category(db_session, admin_user)
        res = await client.post(
            f"/api/v1/elections/{election.election_id}/nomination-forms",
            json=FORM_PAYLOAD,
            headers=admin_user["headers"],
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["slug"]
        assert [f["label"] for f in body["fields"]] == ["Why?", "Contact"]
        assert [f["display_order"] for f in body["fields"]] == [0, 1]

    async def test_update_form_reorders_fields_wholesale(self, client: AsyncClient, db_session, admin_user):
        election, _category = await _make_election_with_category(db_session, admin_user)
        create_res = await client.post(
            f"/api/v1/elections/{election.election_id}/nomination-forms",
            json=FORM_PAYLOAD,
            headers=admin_user["headers"],
        )
        form_id = create_res.json()["form_id"]

        reordered = {
            **FORM_PAYLOAD,
            "fields": [
                {"field_type": "short-text", "label": "Contact", "required": True},
                {"field_type": "paragraph", "label": "Why?", "required": False},
            ],
        }
        res = await client.put(
            f"/api/v1/nomination-forms/{form_id}", json=reordered, headers=admin_user["headers"]
        )
        assert res.status_code == 200, res.text
        assert [f["label"] for f in res.json()["fields"]] == ["Contact", "Why?"]

        get_res = await client.get(
            f"/api/v1/nomination-forms/{form_id}", headers=admin_user["headers"]
        )
        assert [f["label"] for f in get_res.json()["fields"]] == ["Contact", "Why?"]

    async def test_outsider_cannot_access_form(self, client: AsyncClient, db_session, admin_user, test_user):
        election, _category = await _make_election_with_category(db_session, admin_user)
        create_res = await client.post(
            f"/api/v1/elections/{election.election_id}/nomination-forms",
            json=FORM_PAYLOAD,
            headers=admin_user["headers"],
        )
        form_id = create_res.json()["form_id"]

        res = await client.get(
            f"/api/v1/nomination-forms/{form_id}", headers=test_user["headers"]
        )
        assert res.status_code == 403


class TestPublicNominationFlow:
    async def test_public_form_exposes_real_categories(self, client: AsyncClient, db_session, admin_user):
        election, category = await _make_election_with_category(db_session, admin_user)
        create_res = await client.post(
            f"/api/v1/elections/{election.election_id}/nomination-forms",
            json=FORM_PAYLOAD,
            headers=admin_user["headers"],
        )
        slug = create_res.json()["slug"]

        res = await client.get(f"/api/v1/public/nomination-forms/{slug}")
        assert res.status_code == 200
        body = res.json()
        assert body["category_options"] == [{"category_id": str(category.category_id), "name": "Best Dressed"}]
        assert len(body["fields"]) == 2

    async def test_submit_creates_public_submission(self, client: AsyncClient, db_session, admin_user):
        election, category = await _make_election_with_category(db_session, admin_user)
        create_res = await client.post(
            f"/api/v1/elections/{election.election_id}/nomination-forms",
            json=FORM_PAYLOAD,
            headers=admin_user["headers"],
        )
        slug = create_res.json()["slug"]

        res = await client.post(
            f"/api/v1/public/nomination-forms/{slug}/submit",
            json={"category_id": str(category.category_id), "nominee_name": "Yaw Boateng", "answers": {}},
        )
        assert res.status_code == 201, res.text

        groups = await client.get(
            f"/api/v1/elections/{election.election_id}/nomination-groups",
            headers=admin_user["headers"],
        )
        assert groups.status_code == 200
        group = groups.json()[0]
        assert group["nominee_name"] == "Yaw Boateng"
        assert group["public_count"] == 1
        assert group["private_count"] == 0
        assert group["manual_count"] == 0
        assert group["status"] == "pending"


class TestCrossElectionNominationGroups:
    async def test_lists_groups_across_all_owned_elections(
        self, client: AsyncClient, db_session, admin_user
    ):
        election_a, category_a = await _make_election_with_category(db_session, admin_user)
        election_b, category_b = await _make_election_with_category(db_session, admin_user)
        await client.post(
            f"/api/v1/elections/{election_a.election_id}/nomination-groups/manual",
            json={"category_id": str(category_a.category_id), "nominee_name": "From Election A"},
            headers=admin_user["headers"],
        )
        await client.post(
            f"/api/v1/elections/{election_b.election_id}/nomination-groups/manual",
            json={"category_id": str(category_b.category_id), "nominee_name": "From Election B"},
            headers=admin_user["headers"],
        )

        res = await client.get("/api/v1/nomination-groups", headers=admin_user["headers"])
        assert res.status_code == 200, res.text
        names = {g["nominee_name"]: g["election_id"] for g in res.json()}
        assert names["From Election A"] == str(election_a.election_id)
        assert names["From Election B"] == str(election_b.election_id)

    async def test_outsider_sees_none_of_it(
        self, client: AsyncClient, db_session, admin_user
    ):
        from app.core.security import create_access_token, hash_password
        from app.models.user import Role, User, UserRole
        from sqlalchemy import select as sa_select

        election, category = await _make_election_with_category(db_session, admin_user)
        await client.post(
            f"/api/v1/elections/{election.election_id}/nomination-groups/manual",
            json={"category_id": str(category.category_id), "nominee_name": "Private To Admin"},
            headers=admin_user["headers"],
        )

        outsider = User(
            email="nomination-outsider@example.com", password_hash=hash_password("Passw0rd!"),
            full_name="Outsider", email_verified=True, account_status="active",
        )
        db_session.add(outsider)
        await db_session.flush()
        role = (await db_session.execute(sa_select(Role).where(Role.role_name == "Election Administrator"))).scalar_one_or_none()
        if not role:
            role = Role(role_name="Election Administrator", permissions={})
            db_session.add(role)
            await db_session.flush()
        db_session.add(UserRole(user_id=outsider.user_id, role_id=role.role_id))
        await db_session.flush()
        outsider_headers = {"Authorization": f"Bearer {create_access_token(str(outsider.user_id))}"}

        res = await client.get("/api/v1/nomination-groups", headers=outsider_headers)
        assert res.status_code == 200
        assert all(g["nominee_name"] != "Private To Admin" for g in res.json())


class TestNominationSubmissionDetail:
    async def test_lists_submissions_with_resolved_answers(
        self, client: AsyncClient, db_session, admin_user
    ):
        election, category = await _make_election_with_category(db_session, admin_user)
        create_res = await client.post(
            f"/api/v1/elections/{election.election_id}/nomination-forms",
            json=FORM_PAYLOAD,
            headers=admin_user["headers"],
        )
        form = create_res.json()
        slug = form["slug"]
        why_field = next(f for f in form["fields"] if f["label"] == "Why?")
        contact_field = next(f for f in form["fields"] if f["label"] == "Contact")

        await client.post(
            f"/api/v1/public/nomination-forms/{slug}/submit",
            json={
                "category_id": str(category.category_id),
                "nominee_name": "Yaw Boateng",
                "answers": {
                    why_field["field_id"]: "They led the community project this year.",
                    contact_field["field_id"]: "yaw@example.com",
                },
            },
        )

        res = await client.get(
            f"/api/v1/elections/{election.election_id}/nomination-groups/submissions",
            params={"category_id": str(category.category_id), "nominee_name": "Yaw Boateng"},
            headers=admin_user["headers"],
        )
        assert res.status_code == 200, res.text
        submissions = res.json()
        assert len(submissions) == 1
        answers = {a["label"]: a["value"] for a in submissions[0]["answers"]}
        assert answers["Why?"] == "They led the community project this year."
        assert answers["Contact"] == "yaw@example.com"
        assert submissions[0]["source"] == "public"
        assert submissions[0]["form_title"] == "2026 Nominee Form"

    async def test_manual_submission_has_no_answers(
        self, client: AsyncClient, db_session, admin_user
    ):
        election, category = await _make_election_with_category(db_session, admin_user)
        await client.post(
            f"/api/v1/elections/{election.election_id}/nomination-groups/manual",
            json={"category_id": str(category.category_id), "nominee_name": "Kwesi Amoah"},
            headers=admin_user["headers"],
        )

        res = await client.get(
            f"/api/v1/elections/{election.election_id}/nomination-groups/submissions",
            params={"category_id": str(category.category_id), "nominee_name": "Kwesi Amoah"},
            headers=admin_user["headers"],
        )
        assert res.status_code == 200
        assert res.json()[0]["answers"] == []
        assert res.json()[0]["form_title"] is None

    async def test_outsider_cannot_view_submissions(
        self, client: AsyncClient, db_session, admin_user, test_user
    ):
        election, category = await _make_election_with_category(db_session, admin_user)
        await client.post(
            f"/api/v1/elections/{election.election_id}/nomination-groups/manual",
            json={"category_id": str(category.category_id), "nominee_name": "Kwesi Amoah"},
            headers=admin_user["headers"],
        )

        res = await client.get(
            f"/api/v1/elections/{election.election_id}/nomination-groups/submissions",
            params={"category_id": str(category.category_id), "nominee_name": "Kwesi Amoah"},
            headers=test_user["headers"],
        )
        assert res.status_code == 403


class TestNominationReview:
    async def test_manual_add_then_approve_creates_candidate(self, client: AsyncClient, db_session, admin_user):
        election, category = await _make_election_with_category(db_session, admin_user)

        manual_res = await client.post(
            f"/api/v1/elections/{election.election_id}/nomination-groups/manual",
            json={"category_id": str(category.category_id), "nominee_name": "Kwesi Amoah"},
            headers=admin_user["headers"],
        )
        assert manual_res.status_code == 201, manual_res.text

        groups = await client.get(
            f"/api/v1/elections/{election.election_id}/nomination-groups",
            headers=admin_user["headers"],
        )
        group = groups.json()[0]
        assert group["manual_count"] == 1

        review_res = await client.post(
            f"/api/v1/elections/{election.election_id}/nomination-groups/review",
            json={"category_id": str(category.category_id), "nominee_name": "Kwesi Amoah", "status": "approved"},
            headers=admin_user["headers"],
        )
        assert review_res.status_code == 200, review_res.text
        assert review_res.json()["candidate_id"] is not None

        candidates_res = await client.get(
            f"/api/v1/elections/{election.election_id}/candidates", headers=admin_user["headers"]
        )
        assert any(c["name"] == "Kwesi Amoah" for c in candidates_res.json())

        # Re-approving the same group must not create a second Candidate.
        review_again = await client.post(
            f"/api/v1/elections/{election.election_id}/nomination-groups/review",
            json={"category_id": str(category.category_id), "nominee_name": "Kwesi Amoah", "status": "approved"},
            headers=admin_user["headers"],
        )
        assert review_again.json()["candidate_id"] == review_res.json()["candidate_id"]
        candidates_after = await client.get(
            f"/api/v1/elections/{election.election_id}/candidates", headers=admin_user["headers"]
        )
        assert len([c for c in candidates_after.json() if c["name"] == "Kwesi Amoah"]) == 1

    async def test_reject_does_not_create_candidate(self, client: AsyncClient, db_session, admin_user):
        election, category = await _make_election_with_category(db_session, admin_user)
        await client.post(
            f"/api/v1/elections/{election.election_id}/nomination-groups/manual",
            json={"category_id": str(category.category_id), "nominee_name": "Ama Serwaa"},
            headers=admin_user["headers"],
        )
        review_res = await client.post(
            f"/api/v1/elections/{election.election_id}/nomination-groups/review",
            json={"category_id": str(category.category_id), "nominee_name": "Ama Serwaa", "status": "rejected"},
            headers=admin_user["headers"],
        )
        assert review_res.status_code == 200
        assert review_res.json()["candidate_id"] is None

        groups = await client.get(
            f"/api/v1/elections/{election.election_id}/nomination-groups",
            headers=admin_user["headers"],
        )
        assert all(g["status"] != "approved" for g in groups.json())


class TestCrossElectionNominationForms:
    async def test_lists_forms_across_all_owned_elections_with_titles(
        self, client: AsyncClient, db_session, admin_user
    ):
        election_a, _category_a = await _make_election_with_category(db_session, admin_user)
        election_b, _category_b = await _make_election_with_category(db_session, admin_user)
        await client.post(
            f"/api/v1/elections/{election_a.election_id}/nomination-forms",
            json={**FORM_PAYLOAD, "title": "Form A"},
            headers=admin_user["headers"],
        )
        await client.post(
            f"/api/v1/elections/{election_b.election_id}/nomination-forms",
            json={**FORM_PAYLOAD, "title": "Form B"},
            headers=admin_user["headers"],
        )

        res = await client.get("/api/v1/nomination-forms/mine", headers=admin_user["headers"])
        assert res.status_code == 200, res.text
        titles = {f["title"]: f["election_title"] for f in res.json()}
        assert titles["Form A"] == election_a.title
        assert titles["Form B"] == election_b.title

    async def test_outsider_sees_none_of_it(
        self, client: AsyncClient, db_session, admin_user
    ):
        from app.core.security import create_access_token, hash_password
        from app.models.user import Role, User, UserRole
        from sqlalchemy import select as sa_select

        election, _category = await _make_election_with_category(db_session, admin_user)
        await client.post(
            f"/api/v1/elections/{election.election_id}/nomination-forms",
            json={**FORM_PAYLOAD, "title": "Private Form"},
            headers=admin_user["headers"],
        )

        outsider = User(
            email="nomination-form-outsider@example.com", password_hash=hash_password("Passw0rd!"),
            full_name="Outsider", email_verified=True, account_status="active",
        )
        db_session.add(outsider)
        await db_session.flush()
        role = (await db_session.execute(sa_select(Role).where(Role.role_name == "Election Administrator"))).scalar_one_or_none()
        if not role:
            role = Role(role_name="Election Administrator", permissions={})
            db_session.add(role)
            await db_session.flush()
        db_session.add(UserRole(user_id=outsider.user_id, role_id=role.role_id))
        await db_session.flush()
        outsider_headers = {"Authorization": f"Bearer {create_access_token(str(outsider.user_id))}"}

        res = await client.get("/api/v1/nomination-forms/mine", headers=outsider_headers)
        assert res.status_code == 200
        assert all(f["title"] != "Private Form" for f in res.json())
