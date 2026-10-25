"""Pure per-transaction money math for Financial Stats — no I/O, so the
commission/fee/channel rules are trivially unit-testable on their own."""
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from app.core.plans import ESTIMATED_PROCESSING_RATE_BPS, TICKET_COMMISSION_RATE_BPS, commission_rate_bps


def _q(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _fee_from_charged_amount(amount_ghs: Decimal, bps: int, fee_payer: str) -> Decimal:
    """Default (`fee_payer="organizer"`): amount_ghs is the sticker price,
    fee is a straight percentage of it. `fee_payer="buyer"`: amount_ghs is
    sticker price *plus* the surcharge already added on top, so the fee is
    backed out of the total instead — `amount_ghs * bps/10000` would
    overstate it, since it'd tax the surcharge itself, not just the base."""
    if fee_payer == "buyer" and bps:
        base = amount_ghs / (Decimal(1) + Decimal(bps) / Decimal(10000))
        return _q(amount_ghs - base)
    return _q(amount_ghs * bps / Decimal(10000))


def vote_platform_fee_ghs(
    amount_ghs: Decimal, plan: str, override_bps: Optional[int], fee_payer: str = "organizer"
) -> Decimal:
    bps = commission_rate_bps(plan, override_bps)
    return _fee_from_charged_amount(amount_ghs, bps, fee_payer)


def ticket_platform_fee_ghs(
    amount_ghs: Decimal, plan: str, override_bps: Optional[int], fee_payer: str = "organizer"
) -> Decimal:
    bps = commission_rate_bps(plan, override_bps, rate_table=TICKET_COMMISSION_RATE_BPS)
    return _fee_from_charged_amount(amount_ghs, bps, fee_payer)


def buyer_facing_vote_price_pesewas(
    base_pesewas: int, plan: str, override_bps: Optional[int], fee_payer: str
) -> int:
    """What the buyer is actually charged per vote. Unchanged (== base) when
    the organizer absorbs the fee, the default; with `fee_payer == "buyer"`
    the commission is added on top so the organizer still nets the full
    base price."""
    if fee_payer != "buyer":
        return base_pesewas
    fee_ghs = vote_platform_fee_ghs(Decimal(base_pesewas) / 100, plan, override_bps)
    return base_pesewas + int(fee_ghs * 100)


def buyer_facing_ticket_price_ghs(
    base_ghs: Decimal, plan: str, override_bps: Optional[int], fee_payer: str
) -> Decimal:
    """Ticket-price equivalent of buyer_facing_vote_price_pesewas — cedis
    rather than pesewas since ticket prices are stored as Decimal GHS."""
    if fee_payer != "buyer":
        return base_ghs
    return base_ghs + ticket_platform_fee_ghs(base_ghs, plan, override_bps)


def processing_fee_ghs(paystack_response: Optional[dict], amount_ghs: Decimal) -> Decimal:
    """The real fee Paystack reported (in kobo/pesewas) when the stored
    response has one, else a fixed-rate estimate. Never fabricated as
    itemized-looking data an organizer could mistake for Paystack's own
    invoice — this is explicitly an estimate when it falls back."""
    if paystack_response:
        raw_fees = paystack_response.get("fees")
        if raw_fees is not None:
            try:
                return _q(Decimal(str(raw_fees)) / 100)
            except (ValueError, ArithmeticError):
                pass
    return _q(amount_ghs * ESTIMATED_PROCESSING_RATE_BPS / Decimal(10000))


def extract_channel(paystack_response: Optional[dict]) -> str:
    """Card / Mobile Money / Bank / Other — only what Paystack's `channel`
    field reliably reports. Deliberately not network-level (MTN/Vodafone/
    AirtelTigo) — that data isn't consistently present."""
    if not paystack_response:
        return "Unknown"
    channel = (paystack_response.get("channel") or "").lower()
    if channel == "card":
        return "Card"
    if channel == "mobile_money":
        return "Mobile Money"
    if channel in ("bank", "bank_transfer", "eft"):
        return "Bank"
    return "Other" if channel else "Unknown"
