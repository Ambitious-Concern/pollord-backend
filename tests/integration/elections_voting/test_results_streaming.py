import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis
from app.core.results_stream import notify_vote_cast, stream_results
from app.models.election import Candidate, Category, Election
from app.schemas.vote import ElectionResults

# The full HTTP round trip (StreamingResponse over a real connection, pushed
# by a real vote cast) is verified manually against a live uvicorn process —
# httpx's in-process ASGITransport has a documented quirk where it never
# signals client-disconnect until its own response context manager exits,
# which deadlocks against an intentionally-infinite SSE stream under
# asyncio-level cancellation. The pieces below (publish-on-vote, and the
# generator's subscribe/push/cache-prime/cleanup logic) are what that manual
# run can't repeat automatically, so they're what's covered here instead.


class _StubResults:
    """Minimal stand-in matching the (model, compute) contract stream_results
    expects, without touching a real database — isolates the generator's own
    subscribe/push/cleanup logic from the DB layer."""

    def __init__(self, first: ElectionResults, second: ElectionResults):
        self._values = [first, second]

    async def compute(self) -> ElectionResults:
        return self._values.pop(0) if len(self._values) > 1 else self._values[0]


def _results(total_votes: int) -> ElectionResults:
    return ElectionResults(
        election_id=uuid4(),
        title="Stub",
        total_votes=total_votes,
        total_eligible_voters=0,
        turnout_percentage=0,
        status="active",
        categories=[],
    )


class TestNotifyVoteCastPublishes:
    async def test_cast_vote_publishes_to_channel(self, client: AsyncClient, db_session: AsyncSession, admin_user):
        now = datetime.now(timezone.utc)
        election = Election(
            title="Publish Test Election",
            start_datetime=now - timedelta(hours=1),
            end_datetime=now + timedelta(hours=1),
            status="active",
            visibility="public",
            require_verification=False,
            vote_price=0,
            created_by=admin_user["user"].user_id,
        )
        db_session.add(election)
        await db_session.flush()
        category = Category(
            election_id=election.election_id, name="President",
            election_type="single_choice", display_order=0,
        )
        db_session.add(category)
        await db_session.flush()
        candidate = Candidate(
            category_id=category.category_id, election_id=election.election_id,
            name="Anne", short_code="ANNE", display_order=0,
        )
        db_session.add(candidate)
        await db_session.flush()

        redis = await get_redis()
        pubsub = redis.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(f"live_results_updates:election:{election.election_id}")

        res = await client.post(
            "/api/v1/voting/public/cast",
            json={"category_id": str(category.category_id), "candidate_ids": [str(candidate.candidate_id)]},
        )
        assert res.status_code == 201

        # get_message() can legitimately return None on a given poll (e.g. it
        # flushed a buffered subscribe-confirmation with nothing to yield) —
        # unlike listen()'s internal loop, it doesn't retry on our behalf, so
        # a fixed number of polls is needed rather than stopping at the
        # first None.
        message = None
        for _ in range(10):
            message = await pubsub.get_message(timeout=1)
            if message is not None and message["type"] == "message":
                break
        assert message is not None
        assert message["type"] == "message"
        assert message["data"] == "updated"

        await pubsub.unsubscribe()
        await pubsub.aclose()


class TestStreamResultsGenerator:
    async def test_pushes_initial_then_update_and_primes_cache(self):
        election_id = uuid4()
        first = _results(0)
        second = _results(1)
        stub = _StubResults(first, second)

        gen = stream_results("election", election_id, ElectionResults, stub.compute)

        first_chunk = await gen.__anext__()
        assert json.loads(first_chunk.removeprefix("data: ").strip())["total_votes"] == 0

        cache_key = f"live_results:election:{election_id}"
        redis = await get_redis()
        cached = await redis.get(cache_key)
        assert json.loads(cached)["total_votes"] == 0

        await notify_vote_cast(election_id, None)
        second_chunk = await asyncio.wait_for(gen.__anext__(), timeout=3)
        assert json.loads(second_chunk.removeprefix("data: ").strip())["total_votes"] == 1

        cached = await redis.get(cache_key)
        assert json.loads(cached)["total_votes"] == 1

        await gen.aclose()
