from typing import Dict, List, Optional, Tuple
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ownership import require_election_ownership
from app.core.slug import generate_slug
from app.models.election import Election
from app.models.event import Event
from app.models.nomination import NominationForm, NominationSubmission
from app.models.notification import Notification
from app.models.org_activity_log import OrgActivityLog
from app.models.organization import Organization
from app.models.user import User
from app.repositories.election_repository import CandidateRepository, CategoryRepository, ElectionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.nomination_repository import NominationFormRepository, NominationSubmissionRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.org_activity_log_repository import OrgActivityLogRepository
from app.repositories.organization_repository import OrganizationRepository
from app.schemas.nomination import (
    ManualNominationCreate,
    NominationAnswerResponse,
    NominationFormCreate,
    NominationFormFieldResponse,
    NominationFormResponse,
    NominationFormSlugResponse,
    NominationFormUpdate,
    NominationGroupResponse,
    NominationGroupReview,
    NominationSubmissionCreate,
    NominationSubmissionResponse,
    PublicNominationFormResponse,
)


class NominationService:
    """The full nomination pipeline: form CRUD, public submission capture,
    and the review queue (approve turns a group into a real Candidate)."""

    def __init__(
        self,
        db: AsyncSession,
        form_repo: NominationFormRepository,
        submission_repo: NominationSubmissionRepository,
        election_repo: ElectionRepository,
        category_repo: CategoryRepository,
        candidate_repo: CandidateRepository,
        event_repo: Optional[EventRepository] = None,
    ):
        self.db = db
        self.form_repo = form_repo
        self.submission_repo = submission_repo
        self.election_repo = election_repo
        self.category_repo = category_repo
        self.candidate_repo = candidate_repo
        self.event_repo = event_repo

    async def _require_election(self, election_id: UUID, user: User) -> Election:
        election = await self.election_repo.get_by_id(election_id, id_field="election_id")
        if not election:
            raise HTTPException(status_code=404, detail="Election not found")
        await require_election_ownership(election, user, self.db)
        return election

    async def _require_event(self, event_id: UUID, user: User) -> Event:
        if self.event_repo is None:
            raise HTTPException(status_code=500, detail="Event repository not configured")
        event = await self.event_repo.get_by_id(event_id, id_field="event_id")
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")
        if event.created_by != user.user_id:
            raise HTTPException(status_code=403, detail="Not authorized")
        return event

    async def _require_form_ownership(self, form: NominationForm, user: User):
        """Check ownership for a form that may belong to either an election or event."""
        if form.election_id:
            await self._require_election(form.election_id, user)
        else:
            await self._require_event(form.event_id, user)

    @staticmethod
    def _to_response(form, base_url: str, election_title: str = "") -> NominationFormResponse:
        ordered = sorted(form.fields, key=lambda f: f.display_order)
        return NominationFormResponse(
            form_id=form.form_id,
            election_id=form.election_id,
            event_id=form.event_id,
            title=form.title,
            description=form.description,
            visibility=form.visibility,
            slug=form.slug,
            url=f"{base_url.rstrip('/')}/nominate/{form.slug}",
            election_title=election_title,
            fields=[NominationFormFieldResponse.model_validate(f) for f in ordered],
            created_at=form.created_at,
            updated_at=form.updated_at,
        )

    # --- Organizer: form builder ----------------------------------------

    async def create_form(
        self, election_id: UUID, user: User, data: NominationFormCreate, base_url: str
    ) -> NominationFormResponse:
        await self._require_election(election_id, user)
        form = await self.form_repo.create(
            {
                "election_id": election_id,
                "title": data.title,
                "description": data.description,
                "visibility": data.visibility,
                "slug": generate_slug(data.title),
                "created_by": user.user_id,
            }
        )
        await self.form_repo.replace_fields(form.form_id, [f.model_dump() for f in data.fields])
        return self._to_response(await self.form_repo.get_with_fields(form.form_id), base_url)

    async def update_form(
        self, form_id: UUID, user: User, data: NominationFormUpdate, base_url: str
    ) -> NominationFormResponse:
        form = await self.form_repo.get_with_fields(form_id)
        if not form:
            raise HTTPException(status_code=404, detail="Nomination form not found")
        await self._require_form_ownership(form, user)

        await self.form_repo.update(
            form_id,
            {"title": data.title, "description": data.description, "visibility": data.visibility},
            id_field="form_id",
        )
        await self.form_repo.replace_fields(form_id, [f.model_dump() for f in data.fields])
        return self._to_response(await self.form_repo.get_with_fields(form_id), base_url)

    async def list_forms(
        self, election_id: UUID, user: User, base_url: str
    ) -> List[NominationFormResponse]:
        await self._require_election(election_id, user)
        forms = await self.form_repo.get_by_election(election_id)
        return [self._to_response(f, base_url) for f in forms]

    async def list_forms_for_teammates(
        self, teammate_ids: List[UUID], base_url: str
    ) -> List[NominationFormResponse]:
        """Every nomination form across every election+event the caller's team owns."""
        election_rows = (
            await self.db.execute(
                select(Election.election_id, Election.title).where(Election.created_by.in_(teammate_ids))
            )
        ).all()
        election_titles: Dict[UUID, str] = {r.election_id: r.title for r in election_rows}

        event_rows = (
            await self.db.execute(
                select(Event.event_id, Event.title).where(Event.created_by.in_(teammate_ids))
            )
        ).all()
        event_titles: Dict[UUID, str] = {r.event_id: r.title for r in event_rows}

        election_forms = (
            await self.form_repo.get_by_elections(list(election_titles.keys()))
            if election_titles else []
        )
        event_forms = (
            await self.form_repo.get_by_events(list(event_titles.keys()))
            if event_titles else []
        )

        responses = [
            self._to_response(f, base_url, election_titles.get(f.election_id, ""))
            for f in election_forms
        ]
        responses += [
            self._to_response(f, base_url, event_titles.get(f.event_id, ""))
            for f in event_forms
        ]
        return responses

    async def create_form_for_event(
        self, event_id: UUID, user: User, data: NominationFormCreate, base_url: str
    ) -> NominationFormResponse:
        await self._require_event(event_id, user)
        form = await self.form_repo.create(
            {
                "event_id": event_id,
                "title": data.title,
                "description": data.description,
                "visibility": data.visibility,
                "slug": generate_slug(data.title),
                "created_by": user.user_id,
            }
        )
        await self.form_repo.replace_fields(form.form_id, [f.model_dump() for f in data.fields])
        return self._to_response(await self.form_repo.get_with_fields(form.form_id), base_url)

    async def list_forms_for_event(
        self, event_id: UUID, user: User, base_url: str
    ) -> List[NominationFormResponse]:
        await self._require_event(event_id, user)
        forms = await self.form_repo.get_by_event(event_id)
        return [self._to_response(f, base_url) for f in forms]

    async def get_form(self, form_id: UUID, user: User, base_url: str) -> NominationFormResponse:
        form = await self.form_repo.get_with_fields(form_id)
        if not form:
            raise HTTPException(status_code=404, detail="Nomination form not found")
        await self._require_form_ownership(form, user)
        return self._to_response(form, base_url)

    async def delete_form(self, form_id: UUID, user: User) -> None:
        form = await self.form_repo.get_with_fields(form_id)
        if not form:
            raise HTTPException(status_code=404, detail="Nomination form not found")
        await self._require_form_ownership(form, user)
        await self.form_repo.delete(form_id, id_field="form_id")

    # --- Public: viewing + submitting ------------------------------------

    async def get_public_form(self, slug: str) -> PublicNominationFormResponse:
        form = await self.form_repo.get_by_slug(slug)
        if not form:
            raise HTTPException(status_code=404, detail="Form not found")
        if form.election_id:
            categories = await self.category_repo.get_by_election(form.election_id)
            parent = await self.election_repo.get_by_id(form.election_id, id_field="election_id")
        else:
            categories = await self.category_repo.get_by_event(form.event_id)
            parent = await self.event_repo.get_by_id(form.event_id, id_field="event_id") if self.event_repo else None

        listing_slug = parent.slug if parent else None

        ordered = sorted(form.fields, key=lambda f: f.display_order)
        return PublicNominationFormResponse(
            form_id=form.form_id,
            title=form.title,
            description=form.description,
            category_options=[{"category_id": str(c.category_id), "name": c.name} for c in categories],
            fields=[NominationFormFieldResponse.model_validate(f) for f in ordered],
            listing_slug=listing_slug,
        )

    async def get_form_for_listing(
        self, listing_type: str, listing_id: UUID
    ) -> Optional[NominationFormSlugResponse]:
        """Returns the first public form for a listing's Nominate button, or None."""
        if listing_type == "election":
            form = await self.form_repo.get_first_public_by_election(listing_id)
        else:
            form = await self.form_repo.get_first_public_by_event(listing_id)
        if not form:
            return None
        return NominationFormSlugResponse(slug=form.slug, title=form.title)

    async def submit_public(
        self,
        slug: str,
        data: NominationSubmissionCreate,
        ip: Optional[str],
        user_agent: Optional[str],
    ) -> NominationSubmission:
        form = await self.form_repo.get_by_slug(slug)
        if not form:
            raise HTTPException(status_code=404, detail="Form not found")

        category = await self.category_repo.get_by_id(data.category_id, id_field="category_id")
        if form.election_id:
            if not category or category.election_id != form.election_id:
                raise HTTPException(status_code=400, detail="Invalid category for this election")
        else:
            if not category or category.event_id != form.event_id:
                raise HTTPException(status_code=400, detail="Invalid category for this event")

        submission = await self.submission_repo.create(
            {
                "form_id": form.form_id,
                "election_id": form.election_id,
                "event_id": form.event_id,
                "category_id": data.category_id,
                "nominee_name": data.nominee_name.strip(),
                "source": form.visibility,
                "answers": data.answers,
                "submitter_ip": ip,
                "submitter_user_agent": user_agent,
            }
        )

        org_id = None
        if form.election_id:
            parent = await self.election_repo.get_by_id(form.election_id, id_field="election_id")
            if parent:
                org_id = parent.org_id
        elif self.event_repo and form.event_id:
            parent = await self.event_repo.get_by_id(form.event_id, id_field="event_id")
            if parent:
                org_id = parent.org_id

        if org_id is not None:
            member_ids = await OrganizationRepository(Organization, self.db).get_member_ids(org_id)
            await NotificationRepository(Notification, self.db).notify_org(
                org_id=org_id,
                member_ids=member_ids,
                type="nomination",
                title="New nomination submitted",
                description=f'"{submission.nominee_name}" was nominated for {category.name}.',
                link="/dashboard/elections/nominations",
            )

        return submission

    # --- Organizer: manual add + review queue -----------------------------

    async def add_manual(
        self, election_id: UUID, user: User, data: ManualNominationCreate
    ) -> NominationSubmission:
        await self._require_election(election_id, user)
        category = await self.category_repo.get_by_id(data.category_id, id_field="category_id")
        if not category or category.election_id != election_id:
            raise HTTPException(status_code=400, detail="Invalid category for this election")

        return await self.submission_repo.create(
            {
                "form_id": None,
                "election_id": election_id,
                "category_id": data.category_id,
                "nominee_name": data.nominee_name.strip(),
                "source": "manual",
                "answers": {},
            }
        )

    async def list_groups(self, election_id: UUID, user: User) -> List[NominationGroupResponse]:
        election = await self._require_election(election_id, user)
        submissions = await self.submission_repo.get_by_election(election_id)
        category_names = {c.category_id: c.name for c in await self.category_repo.get_by_election(election_id)}
        return self._group_submissions(submissions, {election_id: election.title}, category_names)

    async def list_groups_for_teammates(self, teammate_ids: List[UUID]) -> List[NominationGroupResponse]:
        """Every nomination group across every election+event the caller's team owns."""
        election_rows = (
            await self.db.execute(
                select(Election.election_id, Election.title).where(Election.created_by.in_(teammate_ids))
            )
        ).all()
        election_titles: Dict[UUID, str] = {r.election_id: r.title for r in election_rows}

        event_rows = (
            await self.db.execute(
                select(Event.event_id, Event.title).where(Event.created_by.in_(teammate_ids))
            )
        ).all()
        event_titles: Dict[UUID, str] = {r.event_id: r.title for r in event_rows}

        election_submissions = (
            await self.submission_repo.get_by_elections(list(election_titles.keys()))
            if election_titles else []
        )
        event_submissions = (
            await self.submission_repo.get_by_events(list(event_titles.keys()))
            if event_titles else []
        )
        submissions = election_submissions + event_submissions
        if not submissions:
            return []

        election_categories = (
            await self.category_repo.get_by_elections(list(election_titles.keys()))
            if election_titles else []
        )
        event_categories = (
            await self.category_repo.get_by_events(list(event_titles.keys()))
            if event_titles else []
        )
        category_names = {c.category_id: c.name for c in election_categories + event_categories}
        return self._group_submissions(submissions, election_titles, category_names, event_titles)

    @staticmethod
    def _group_submissions(
        submissions: List[NominationSubmission],
        election_titles: Dict[UUID, str],
        category_names: Dict[UUID, str],
        event_titles: Optional[Dict[UUID, str]] = None,
    ) -> List[NominationGroupResponse]:
        groups: Dict[Tuple, dict] = {}
        for s in submissions:
            parent_id = s.election_id or s.event_id
            key = (parent_id, s.category_id, s.nominee_name)
            title = (
                election_titles.get(s.election_id, "")
                if s.election_id
                else (event_titles or {}).get(s.event_id, "")
            )
            group = groups.setdefault(
                key,
                {
                    "election_id": s.election_id,
                    "event_id": s.event_id,
                    "election_title": title,
                    "category_id": s.category_id,
                    "category_name": category_names.get(s.category_id, ""),
                    "nominee_name": s.nominee_name,
                    "public_count": 0,
                    "private_count": 0,
                    "manual_count": 0,
                    "first_submitted_at": s.submitted_at,
                    "status": s.status,
                },
            )
            group[f"{s.source}_count"] += 1
            if s.submitted_at < group["first_submitted_at"]:
                group["first_submitted_at"] = s.submitted_at
            group["status"] = s.status

        return [NominationGroupResponse(**g) for g in groups.values()]

    async def list_submissions_for_group(
        self, election_id: UUID, user: User, category_id: UUID, nominee_name: str
    ) -> List[NominationSubmissionResponse]:
        """The individual submissions behind one NominationGroupResponse row
        — what a reviewer opens to actually read a nominator's reasoning
        against the real form they were given."""
        await self._require_election(election_id, user)
        submissions = await self.submission_repo.get_group(election_id, category_id, nominee_name)

        forms_by_id = {}
        responses: List[NominationSubmissionResponse] = []
        for s in submissions:
            answers: List[NominationAnswerResponse] = []
            form_title = None
            if s.form_id is not None:
                if s.form_id not in forms_by_id:
                    forms_by_id[s.form_id] = await self.form_repo.get_with_fields(s.form_id)
                form = forms_by_id[s.form_id]
                if form is not None:
                    form_title = form.title
                    for field in sorted(form.fields, key=lambda f: f.display_order):
                        answers.append(
                            NominationAnswerResponse(
                                field_id=field.field_id,
                                label=field.label,
                                field_type=field.field_type,
                                value=s.answers.get(str(field.field_id)),
                            )
                        )
            responses.append(
                NominationSubmissionResponse(
                    submission_id=s.submission_id,
                    category_id=s.category_id,
                    nominee_name=s.nominee_name,
                    source=s.source,
                    status=s.status,
                    form_title=form_title,
                    answers=answers,
                    submitted_at=s.submitted_at,
                )
            )
        return sorted(responses, key=lambda r: r.submitted_at)

    async def review_group(
        self,
        election_id: UUID,
        user: User,
        data: NominationGroupReview,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> dict:
        election = await self._require_election(election_id, user)
        submissions = await self.submission_repo.get_group(
            election_id, data.category_id, data.nominee_name
        )
        if not submissions:
            raise HTTPException(
                status_code=404, detail="No submissions found for this nominee/category"
            )

        await self.submission_repo.set_group_status(
            election_id, data.category_id, data.nominee_name, data.status
        )

        candidate_id = None
        if data.status == "approved":
            existing = await self.candidate_repo.get_by_category(data.category_id)
            match = next((c for c in existing if c.name == data.nominee_name), None)
            if match:
                candidate_id = match.candidate_id
            else:
                candidate = await self.candidate_repo.create(
                    {
                        "category_id": data.category_id,
                        "election_id": election_id,
                        "name": data.nominee_name,
                        "display_order": len(existing),
                    }
                )
                candidate_id = candidate.candidate_id
        elif data.status == "rejected":
            await self._delete_photo_answers(submissions)

        if election.org_id is not None:
            category = await self.category_repo.get_by_id(data.category_id, id_field="category_id")
            await OrgActivityLogRepository(OrgActivityLog, self.db).log_activity(
                org_id=election.org_id,
                category="voting",
                verb="approved nominee" if data.status == "approved" else "rejected a nomination for",
                target_name=data.nominee_name,
                user_id=user.user_id,
                detail=f"{category.name if category else ''} · {election.title}",
                ip_address=ip,
                user_agent=user_agent,
            )

        return {
            "status": data.status,
            "candidate_id": candidate_id,
            "submissions_updated": len(submissions),
        }

    async def _delete_photo_answers(self, submissions: List[NominationSubmission]) -> None:
        """Delete stored photo files from all submissions in a rejected group.

        Failures are logged and swallowed — rejection must succeed even if
        storage cleanup fails (e.g. file already deleted, remote service down).
        """
        from app.services.file_storage_service import file_storage_service

        forms_by_id: Dict[UUID, Optional[NominationForm]] = {}
        for submission in submissions:
            if not submission.form_id or not submission.answers:
                continue
            if submission.form_id not in forms_by_id:
                forms_by_id[submission.form_id] = await self.form_repo.get_with_fields(
                    submission.form_id
                )
            form = forms_by_id.get(submission.form_id)
            if form is None:
                continue

            photo_field_ids = {
                str(f.field_id) for f in form.fields if f.field_type == "photo"
            }
            for field_id in photo_field_ids:
                url = submission.answers.get(field_id)
                if not isinstance(url, str):
                    continue
                parts = file_storage_service.extract_path_parts(url)
                if parts:
                    await file_storage_service.delete(*parts)
