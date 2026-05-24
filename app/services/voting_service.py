import logging
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException, status

from app.models.election import Election
from app.models.vote import Vote, VoteReceipt
from app.repositories.audit_log_repository import AuditLogRepository
from app.repositories.election_repository import CandidateRepository, ElectionRepository
from app.repositories.vote_repository import VoteReceiptRepository, VoteRepository
from app.schemas.election import ElectionWithCandidates, CandidateResponse
from app.schemas.vote import CandidateResult, CastVote, ElectionResults, VoteReceiptResponse
from app.services.cryptography_service import CryptographyService

logger = logging.getLogger(__name__)


class VotingService:
    def __init__(
        self,
        election_repo: ElectionRepository,
        candidate_repo: CandidateRepository,
        vote_repo: VoteRepository,
        receipt_repo: VoteReceiptRepository,
        audit_repo: AuditLogRepository,
        crypto_service: CryptographyService,
    ):
        self.election_repo = election_repo
        self.candidate_repo = candidate_repo
        self.vote_repo = vote_repo
        self.receipt_repo = receipt_repo
        self.audit_repo = audit_repo
        self.crypto = crypto_service

    async def cast_vote(
        self,
        user_id: UUID,
        data: CastVote,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> VoteReceiptResponse:
        # 1. Fetch and validate election
        election = await self.election_repo.get_with_candidates(data.election_id)
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

        # 2. Resolve candidate selection (IDs or short codes)
        if data.candidate_short_codes:
            code_map = {
                c.short_code.upper(): c.candidate_id
                for c in election.candidates
                if c.short_code
            }
            resolved = []
            for code in data.candidate_short_codes:
                cid = code_map.get(code.upper())
                if not cid:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Unknown candidate code: {code}",
                    )
                resolved.append(cid)
            candidate_ids = resolved
        elif data.candidate_ids:
            candidate_ids = data.candidate_ids
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Provide either candidate_ids or candidate_short_codes",
            )

        # 3. Verify eligibility — skipped for open public elections
        open_election = (
            election.visibility == "public" and not election.require_verification
        )
        if not open_election:
            is_eligible = await self.election_repo.is_user_eligible(
                data.election_id, user_id
            )
            if not is_eligible:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You are not eligible to vote in this election",
                )

        # 4. Generate voter hash and check duplicate
        allow_revoting = getattr(election, "allow_revoting", False)
        base_hash = self.crypto.generate_voter_hash(user_id, data.election_id)
        if allow_revoting:
            # Mix in a random nonce and re-hash to keep the 64-char size
            import uuid as _uuid
            import hashlib as _hashlib
            voter_hash = _hashlib.sha256(f"{base_hash}:{_uuid.uuid4().hex}".encode()).hexdigest()
        else:
            voter_hash = base_hash
            if await self.vote_repo.has_voted(voter_hash, data.election_id):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="You have already voted in this election",
                )

        # 5. Validate candidate IDs belong to this election
        valid_ids = {c.candidate_id for c in election.candidates}
        for cid in candidate_ids:
            if cid not in valid_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid candidate ID: {cid}",
                )

        # 6. Enforce voting type constraints
        if election.election_type == "single_choice" and len(candidate_ids) != 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Single choice election requires exactly one candidate",
            )

        # 7. Encrypt vote data
        encrypted = self.crypto.encrypt_vote_data(
            [str(cid) for cid in candidate_ids]
        )

        # 8. Sign the vote
        cast_at = now.isoformat()
        signature = self.crypto.sign_vote(encrypted, cast_at)

        # 8. Create vote record
        vote = await self.vote_repo.create(
            {
                "election_id": data.election_id,
                "voter_hash": voter_hash,
                "vote_data": encrypted,
                "vote_signature": signature,
                "count": 1,
            }
        )

        # 9. Create receipt
        receipt_code = self.crypto.generate_receipt_code()
        receipt = await self.receipt_repo.create(
            {
                "user_id": user_id,
                "election_id": data.election_id,
                "receipt_code": receipt_code,
            }
        )

        # 10. Audit log (without vote content)
        await self.audit_repo.log_action(
            action_type="VOTE_CAST",
            entity_type="Election",
            entity_id=data.election_id,
            user_id=user_id,
            ip_address=ip,
            user_agent=user_agent,
        )

        return VoteReceiptResponse(
            receipt_code=receipt.receipt_code,
            election_id=receipt.election_id,
            issued_at=receipt.issued_at,
        )

    async def get_ballot(
        self, user_id: UUID, election_id: UUID
    ) -> ElectionWithCandidates:
        election = await self.election_repo.get_with_candidates(election_id)
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

        return ElectionWithCandidates(
            election_id=election.election_id,
            title=election.title,
            description=election.description,
            election_type=election.election_type,
            start_datetime=election.start_datetime,
            end_datetime=election.end_datetime,
            status=election.status,
            created_by=election.created_by,
            created_at=election.created_at,
            updated_at=election.updated_at,
            candidates=[
                CandidateResponse(
                    candidate_id=c.candidate_id,
                    election_id=c.election_id,
                    name=c.name,
                    short_code=c.short_code,
                    description=c.description,
                    image_url=c.image_url,
                    display_order=c.display_order,
                )
                for c in sorted(election.candidates, key=lambda c: c.display_order)
            ],
        )

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
        election = await self.election_repo.get_with_candidates(election_id)
        if not election:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Election not found",
            )

        votes = await self.vote_repo.get_votes_by_election(election_id)
        candidate_counts: dict[UUID, int] = {}

        for vote in votes:
            weight = getattr(vote, "count", 1)
            decrypted = self.crypto.decrypt_vote_data(vote.vote_data)
            for cid_str in decrypted.get("candidate_ids", []):
                cid = UUID(cid_str)
                candidate_counts[cid] = candidate_counts.get(cid, 0) + weight

        total_votes = sum(getattr(v, "count", 1) for v in votes)
        total_eligible = await self.election_repo.count_eligible_voters(election_id)

        results = []
        for candidate in election.candidates:
            count = candidate_counts.get(candidate.candidate_id, 0)
            percentage = (count / total_votes * 100) if total_votes > 0 else 0
            results.append(
                CandidateResult(
                    candidate_id=candidate.candidate_id,
                    name=candidate.name,
                    vote_count=count,
                    percentage=round(percentage, 2),
                )
            )

        results.sort(key=lambda r: r.vote_count, reverse=True)
        turnout = (total_votes / total_eligible * 100) if total_eligible > 0 else 0

        return ElectionResults(
            election_id=election.election_id,
            title=election.title,
            election_type=election.election_type,
            total_votes=total_votes,
            total_eligible_voters=total_eligible,
            turnout_percentage=round(turnout, 2),
            results=results,
            status=election.status,
        )

    async def get_results(self, election_id: UUID) -> ElectionResults:
        election = await self.election_repo.get_with_candidates(election_id)
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

        # Fetch and decrypt votes
        votes = await self.vote_repo.get_votes_by_election(election_id)
        candidate_counts: dict[UUID, int] = {}

        for vote in votes:
            weight = getattr(vote, "count", 1)
            decrypted = self.crypto.decrypt_vote_data(vote.vote_data)
            for cid_str in decrypted.get("candidate_ids", []):
                cid = UUID(cid_str)
                candidate_counts[cid] = candidate_counts.get(cid, 0) + weight

        total_votes = sum(getattr(v, "count", 1) for v in votes)
        total_eligible = await self.election_repo.count_eligible_voters(election_id)

        results = []
        for candidate in election.candidates:
            count = candidate_counts.get(candidate.candidate_id, 0)
            percentage = (count / total_votes * 100) if total_votes > 0 else 0
            results.append(
                CandidateResult(
                    candidate_id=candidate.candidate_id,
                    name=candidate.name,
                    vote_count=count,
                    percentage=round(percentage, 2),
                )
            )

        results.sort(key=lambda r: r.vote_count, reverse=True)

        turnout = (total_votes / total_eligible * 100) if total_eligible > 0 else 0

        return ElectionResults(
            election_id=election.election_id,
            title=election.title,
            election_type=election.election_type,
            total_votes=total_votes,
            total_eligible_voters=total_eligible,
            turnout_percentage=round(turnout, 2),
            results=results,
            status=election.status,
        )
