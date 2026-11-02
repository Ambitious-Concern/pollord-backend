"""Plan rules shared by every layer that gates a feature or prices a vote.

Chosen per election/event at launch, not an org-wide subscription — see
Pollord_Pricing_Commission_Model_v7.docx.

Plans are now product-based (Voting / Ticketing / Custom) rather than a tier
ladder. Elections default to "voting"; events default to "ticketing".
"""
from decimal import Decimal
from typing import Optional

PLAN_VOTING = "voting"
PLAN_TICKETING = "ticketing"
PLAN_CUSTOM = "custom"

# Backward-compat aliases — callers that imported PLAN_STANDARD / PLAN_PREMIUM
# before the rename continue to work without change.
PLAN_STANDARD = PLAN_VOTING
PLAN_PREMIUM = PLAN_TICKETING

# Order matters only for meets_minimum_plan checks; Custom is still the top
# tier for negotiated volume deals.
PLAN_ORDER = (PLAN_VOTING, PLAN_TICKETING, PLAN_CUSTOM)

# 10% on paid votes (Voting plan elections).
COMMISSION_RATE_BPS = {PLAN_VOTING: 1000, PLAN_TICKETING: 1000}

# 8% on ticket revenue (Ticketing plan events).
TICKET_COMMISSION_RATE_BPS = {PLAN_VOTING: 800, PLAN_TICKETING: 800}

# Fallback used only when a stored Paystack response predates (or omits)
# its own `fees` field — a fixed estimate, not a per-provider rate, since
# Paystack is the only gateway actually integrated.
ESTIMATED_PROCESSING_RATE_BPS = 195


def meets_minimum_plan(plan: str, minimum: str) -> bool:
    return PLAN_ORDER.index(plan) >= PLAN_ORDER.index(minimum)


def commission_rate_bps(
    plan: str, override_bps: Optional[int] = None, rate_table: dict = COMMISSION_RATE_BPS
) -> int:
    """override_bps is the negotiated rate for Custom-plan elections/events;
    ignored for standard/premium, required in practice for custom. A Custom
    deal's single negotiated rate applies uniformly across votes and
    tickets — there's only ever one override_bps field per election/event,
    not a separate one per revenue type."""
    if plan == PLAN_CUSTOM:
        return override_bps if override_bps is not None else 0
    return rate_table[plan]
