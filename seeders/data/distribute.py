"""Splits a fixed total across a fixed number of buckets with variation —
used wherever a domain needs an exact grand total (e.g. "24 categories
across 12 elections") instead of each bucket independently rolling its own
random count, which never sums to anything predictable."""
from seeders.config import rng


def distribute(total: int, buckets: int, minimum: int = 0) -> list[int]:
    """Returns `buckets` non-negative ints, each >= minimum, summing to
    exactly `total`. The leftover above the minimum floor is handed out one
    unit at a time to a random bucket, so counts vary rather than landing
    perfectly even."""
    if buckets <= 0:
        return []
    if total < minimum * buckets:
        raise ValueError(f"total {total} can't satisfy {buckets} buckets at minimum {minimum} each")

    counts = [minimum] * buckets
    for _ in range(total - minimum * buckets):
        counts[rng.randrange(buckets)] += 1
    rng.shuffle(counts)
    return counts
