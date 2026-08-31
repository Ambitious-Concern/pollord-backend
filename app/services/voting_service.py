import logging
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException, status

from app.models.election import Category
from app.models.vote import Vote
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.election_repository import CandidateRepository, CategoryRepository, ElectionRepository
from app.repositories.event_repository import EventRepository
from app.repositories.vote_repository import VoteReceiptRepository, VoteRepository
from app.schemas.election import CandidateResponse, CategoryWithCandidates, ElectionWithCategories
from app.schemas.event import EventWithCategories
from app.schemas.vote import CandidateResult, CastVote, CategoryResults, ElectionResults, EventResults, VoteReceiptResponse
from app.services.cryptography_service import CryptographyService

logger = logging.getLogger(__name__)


def resolve_candidate_selection(
    category: Category,
    candidate_ids: Optional[List[UUID]],
    candidate_short_codes: Optional[List[str]],
) -> List[UUID]:
    """Resolve + validate a candidate selection against one category's candidate
    pool. Parent-agnostic (works the same whether the category belongs to an
    Election or an Event) — shared by the authenticated cast_vote path and the
    public/anonymous voting.py endpoints so this logic isn't hand-duplicated."""
    if candidate_short_codes:
        code_map = {
            c.short_code.upper(): c.candidate_id
            for c in category.candidates
            if c.short_code
        }
        resolved = []
        for code in candidate_short_codes:
            cid = code_map.get(code.upper())
            if not cid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Unknown candidate code: {code}",
                )
            resolved.append(cid)
        resolved_ids = resolved
    elif candidate_ids:
        resolved_ids = candidate_ids
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either candidate_ids or candidate_short_codes",
        )

    valid_ids = {c.candidate_id for c in category.candidates}
    for cid in resolved_ids:
        if cid not in valid_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid candidate ID: {cid}",
            )

    if category.election_type == "single_choice" and len(resolved_ids) != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Single choice requires exactly one candidate",
        )

    return resolved_ids


def tally_votes_by_category(
    votes: List[Vote],
    categories: List[Category],
    crypto: CryptographyService,
) -> List[CategoryResults]:
    """Decrypt every vote and group tallies by category, then by candidate
    within each category. Shared by election and event results/live-results."""
    per_category_counts: dict[UUID, dict[UUID, int]] = {c.category_id: {} for c in categories}

    for vote in votes:
        weight = getattr(vote, "count", 1)
        decrypted = crypto.decrypt_vote_data(vote.vote_data)
        counts = per_category_counts.setdefault(vote.category_id, {})
        for cid_str in decrypted.get("candidate_ids", []):
            cid = UUID(cid_str)
            counts[cid] = counts.get(cid, 0) + weight

    results = []
    for category in categories:
        counts = per_category_counts.get(category.category_id, {})
        total_votes = sum(counts.values())
        candidate_results = []
        for candidate in category.candidates:
            count = counts.get(candidate.candidate_id, 0)
            percentage = (count / total_votes * 100) if total_votes > 0 else 0
            candidate_results.append(
                CandidateResult(
                    candidate_id=candidate.candidate_id,
                    name=candidate.name,
                    vote_count=count,
                    percentage=round(percentage, 2),
                )
            )
        candidate_results.sort(key=lambda r: r.vote_count, reverse=True)
        results.append(
            CategoryResults(
                category_id=category.category_id,
                name=category.name,
                election_type=category.election_type,
                total_votes=total_votes,
                results=candidate_results,
            )
        )
    return results


class VotingService:
    def __init__(
        self,
        election_repo: ElectionRepository,
        event_repo: EventRepository,
        category_repo: CategoryRepository,
        candidate_repo: CandidateRepository,
        vote_repo: VoteRepository,
        receipt_repo: VoteReceiptRepository,
        audit_repo: AuditLogRepository,
        crypto_service: CryptographyService,
    ):
        self.election_repo = election_repo
        self.event_repo = event_repo
        self.category_repo = category_repo
        self.candidate_repo = candidate_repo
        self.vote_repo = vote_repo
        self.receipt_repo = receipt_repo
        self.audit_repo = audit_repo
        self.crypto = crypto_service

    # ------------------------------------------------------------------
    # Authenticated election voting — categories are Election-only here;
    # Events have no authenticated/eligibility-gated flow (see voting.py's
    # public endpoints for the always-open event voting path).
    # ------------------------------------------------------------------

    async def cast_vote(
        self,
        user_id: UUID,
        data: CastVote,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> VoteReceiptResponse:
        category = await self.category_repo.get_with_candidates(data.category_id)
        if not category or category.election_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Category not found",
            )

        election = await self.election_repo.get_by_id(
            category.election_id, id_field="election_id"
        )
        if not election:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Election not found",
            )

        now = datetime.now(timezone.utc)
        if election.status != "active":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Election is not active",
            )
        if now < election.start_datetime or now > election.end_datetime:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Election is not within voting period",
            )

        candidate_ids = resolve_candidate_selection(
            category, data.candidate_ids, data.candidate_short_codes
        )

        open_election = (
            election.visibility == "public" and not election.require_verification
        )
        if not open_election:
            is_eligible = await self.election_repo.is_user_eligible(
                election.election_id, user_id
            )
            if not is_eligible:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You are not eligible to vote in this election",
                )

        # Voter hash is scoped by category_id, not election_id, so the same
        # voter can cast one free vote in each category of this election.
        allow_revoting = getattr(election, "allow_revoting", False)
        base_hash = self.crypto.generate_voter_hash(user_id, data.category_id)
        if allow_revoting:
            import uuid as _uuid
            import hashlib as _hashlib
            voter_hash = _hashlib.sha256(f"{base_hash}:{_uuid.uuid4().hex}".encode()).hexdigest()
        else:
            voter_hash = base_hash
            if await self.vote_repo.has_voted(voter_hash, data.category_id):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="You have already voted in this category",
                )

        encrypted = self.crypto.encrypt_vote_data([str(cid) for cid in candidate_ids])
        cast_at = now.isoformat()
        signature = self.crypto.sign_vote(encrypted, cast_at)

        await self.vote_repo.create(
            {
                "category_id": category.category_id,
                "election_id": election.election_id,
                "voter_hash": voter_hash,
                "vote_data": encrypted,
                "vote_signature": signature,
                "count": 1,
            }
        )

        receipt_code = self.crypto.generate_receipt_code()
        receipt = await self.receipt_repo.create(
            {
                "user_id": user_id,
                "election_id": election.election_id,
                "receipt_code": receipt_code,
            }
        )

        await self.audit_repo.log_action(
            action_type="VOTE_CAST",
            entity_type="Election",
            entity_id=election.election_id,
            user_id=user_id,
            ip_address=ip,
            user_agent=user_agent,
        )

        return VoteReceiptResponse(
            receipt_code=receipt.receipt_code,
            election_id=receipt.election_id,
            issued_at=receipt.issued_at,
        )

    async def get_ballot(self, user_id: UUID, election_id: UUID) -> ElectionWithCategories:
        election = await self.election_repo.get_with_categories(election_id)
        if not election:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Election not found",
            )

        open_election = (
            election.visibility == "public" and not election.require_verification
        )
        if not open_election:
            is_eligible = await self.election_repo.is_user_eligible(
                election_id, user_id
            )
            if not is_eligible:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You are not eligible to vote in this election",
                )

        return self.build_election_with_categories(election)

    async def get_receipt(
        self, user_id: UUID, election_id: UUID
    ) -> VoteReceiptResponse:
        receipt = await self.receipt_repo.get_by_user_and_election(
            user_id, election_id
        )
        if not receipt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Vote receipt not found",
            )

        return VoteReceiptResponse(
            receipt_code=receipt.receipt_code,
            election_id=receipt.election_id,
            issued_at=receipt.issued_at,
        )

    async def get_live_results(self, election_id: UUID) -> ElectionResults:
        """Returns live tallies for any election status (active, scheduled, etc.)."""
        election = await self.election_repo.get_with_categories(election_id)
        if not election:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Election not found",
            )

        votes = await self.vote_repo.get_votes_by_election(election_id)
        category_results = tally_votes_by_category(votes, election.categories, self.crypto)

        total_votes = sum(getattr(v, "count", 1) for v in votes)
        total_eligible = await self.election_repo.count_eligible_voters(election_id)
        turnout = (total_votes / total_eligible * 100) if total_eligible > 0 else 0

        return ElectionResults(
            election_id=election.election_id,
            title=election.title,
            total_votes=total_votes,
            total_eligible_voters=total_eligible,
            turnout_percentage=round(turnout, 2),
            status=election.status,
            categories=category_results,
        )

    async def get_results(self, election_id: UUID) -> ElectionResults:
        election = await self.election_repo.get_with_categories(election_id)
        if not election:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Election not found",
            )

        if election.status not in ("completed", "closed"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Results are only available after the election has ended",
            )

        votes = await self.vote_repo.get_votes_by_election(election_id)
        category_results = tally_votes_by_category(votes, election.categories, self.crypto)

        total_votes = sum(getattr(v, "count", 1) for v in votes)
        total_eligible = await self.election_repo.count_eligible_voters(election_id)
        turnout = (total_votes / total_eligible * 100) if total_eligible > 0 else 0

        return ElectionResults(
            election_id=election.election_id,
            title=election.title,
            total_votes=total_votes,
            total_eligible_voters=total_eligible,
            turnout_percentage=round(turnout, 2),
            status=election.status,
            categories=category_results,
        )

    # ------------------------------------------------------------------
    # Event voting — always open/public, no eligibility or authentication.
    # Casting itself goes through voting.py's anonymous endpoints (mirroring
    # cast_public_vote); these are the read-side (ballot/results) methods.
    # ------------------------------------------------------------------

    async def get_event_ballot(self, event_id: UUID) -> EventWithCategories:
        event = await self.event_repo.get_with_categories(event_id)
        if not event:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Event not found",
            )
        return self.build_event_with_categories(event)

    async def get_event_live_results(self, event_id: UUID) -> EventResults:
        event = await self.event_repo.get_with_categories(event_id)
        if not event:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Event not found",
            )

        votes = await self.vote_repo.get_votes_by_event(event_id)
        category_results = tally_votes_by_category(votes, event.categories, self.crypto)
        total_votes = sum(getattr(v, "count", 1) for v in votes)

        return EventResults(
            event_id=event.event_id,
            title=event.title,
            total_votes=total_votes,
            status=event.status,
            categories=category_results,
        )

    @staticmethod
    def build_election_with_categories(election) -> ElectionWithCategories:
        return ElectionWithCategories(
            election_id=election.election_id,
            title=election.title,
            slug=election.slug,
            description=election.description,
            start_datetime=election.start_datetime,
            end_datetime=election.end_datetime,
            status=election.status,
            created_by=election.created_by,
            created_at=election.created_at,
            updated_at=election.updated_at,
            banner_image_url=election.banner_image_url,
            visibility=election.visibility,
            access_code=election.access_code,
            allow_result_viewing=election.allow_result_viewing,
            require_verification=election.require_verification,
            anonymous_results=election.anonymous_results,
            show_candidate_count=election.show_candidate_count,
            randomize_candidate_order=election.randomize_candidate_order,
            enable_notifications=election.enable_notifications,
            allow_revoting=election.allow_revoting,
            vote_price=election.vote_price,
            venue=election.venue,
            latitude=election.latitude,
            longitude=election.longitude,
            tag=election.tag,
            effective_vote_price=election.vote_price or 100,
            categories=[
                VotingService.build_category_with_candidates(c) for c in election.categories
            ],
        )

    @staticmethod
    def build_event_with_categories(event) -> EventWithCategories:
        return EventWithCategories(
            event_id=event.event_id,
            title=event.title,
            slug=event.slug,
            description=event.description,
            event_date=event.event_date,
            event_time=event.event_time,
            location=event.location,
            category=event.category,
            latitude=event.latitude,
            longitude=event.longitude,
            capacity=event.capacity,
            banner_image_url=event.banner_image_url,
            status=event.status,
            show_ticket_counts=event.show_ticket_counts,
            vote_price=event.vote_price,
            allow_revoting=event.allow_revoting,
            created_by=event.created_by,
            created_at=event.created_at,
            updated_at=event.updated_at,
            effective_vote_price=event.vote_price or 100,
            categories=[
                VotingService.build_category_with_candidates(c) for c in event.categories
            ],
        )

    @staticmethod
    def build_category_with_candidates(category: Category) -> CategoryWithCandidates:
        return CategoryWithCandidates(
            category_id=category.category_id,
            election_id=category.election_id,
            event_id=category.event_id,
            name=category.name,
            description=category.description,
            election_type=category.election_type,
            allow_abstain=category.allow_abstain,
            display_order=category.display_order,
            candidates=[
                CandidateResponse(
                    candidate_id=c.candidate_id,
                    category_id=c.category_id,
                    election_id=c.election_id,
                    event_id=c.event_id,
                    name=c.name,
                    short_code=c.short_code,
                    email=c.email,
                    description=c.description,
                    image_url=c.image_url,
                    display_order=c.display_order,
                )
                for c in sorted(category.candidates, key=lambda c: c.display_order)
            ],
        )
