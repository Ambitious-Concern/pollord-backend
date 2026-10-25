"""Plan rules shared by every layer that gates a feature or prices a vote.

Chosen per election/event at launch, not an org-wide subscription — see
Pollord_Pricing_Commission_Model_v7.docx.
"""
from decimal import Decimal
from typing import Optional

PLAN_STANDARD = "standard"
PLAN_PREMIUM = "premium"
PLAN_CUSTOM = "custom"

# Kept distinct from Standard for plan-ordering purposes (Custom still ranks
# above both), even though the rates below no longer differ between them —
# the redefined pricing model (matching the flat per-product rate every
# comparable Ghanaian platform actually ships) dropped the old tiered-rate
# upsell entirely. Every feature that used to require Premium is now
# unconditional; see app/api/v1/endpoints/front_desk.py, reports.py,
# voting.py, organizations.py, payout_service.py, live_chat_service.py and
# support_service.py.
PLAN_ORDER = (PLAN_STANDARD, PLAN_PREMIUM, PLAN_CUSTOM)

# Basis points (1/100 of a percent). Flat 10% on paid votes regardless of
# Standard/Premium — Custom is still negotiated per deal.
COMMISSION_RATE_BPS = {PLAN_STANDARD: 1000, PLAN_PREMIUM: 1000}

# Ticket commission — a percentage of the ticket price, mirroring the vote
# commission structure (published pricing page). Flat 8% regardless of
# Standard/Premium.
TICKET_COMMISSION_RATE_BPS = {PLAN_STANDARD: 800, PLAN_PREMIUM: 800}

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
