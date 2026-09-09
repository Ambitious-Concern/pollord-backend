"""Vote pricing rules shared by every layer that accepts a price.

Four places used to validate vote_price independently — the global platform
setting, the admin per-election override, the election create/update schema
and the elections endpoint — and three of them disagreed (₵1.00 whole-cedi
steps in two, ₵0.50 steps in the others). Organisers need sub-cedi prices, so
the rule lives here now and everything imports it.
"""
from typing import Optional

# Prices are stored in pesewas. ₵0.10 is the floor; there is deliberately no
# step constraint above it, so ₵0.70 or ₵1.35 are both acceptable.
MIN_VOTE_PRICE_PESEWAS = 10

MIN_VOTE_PRICE_MESSAGE = (
    f"Vote price must be at least {MIN_VOTE_PRICE_PESEWAS} pesewas "
    f"(₵{MIN_VOTE_PRICE_PESEWAS / 100:.2f})"
)


def validate_vote_price(value: Optional[int]) -> Optional[int]:
    """Return the price unchanged, or raise ValueError if it's below the floor.

    None means "inherit the global default" and is always allowed.
    """
    if value is None:
        return value
    if value < MIN_VOTE_PRICE_PESEWAS:
        raise ValueError(MIN_VOTE_PRICE_MESSAGE)
    return value
