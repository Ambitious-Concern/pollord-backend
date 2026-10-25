from decimal import Decimal

from app.core.financial_breakdown import (
    buyer_facing_ticket_price_ghs,
    buyer_facing_vote_price_pesewas,
    extract_channel,
    processing_fee_ghs,
    ticket_platform_fee_ghs,
    vote_platform_fee_ghs,
)


class TestVotePlatformFee:
    def test_standard_plan_is_ten_percent(self):
        assert vote_platform_fee_ghs(Decimal("100.00"), "standard", None) == Decimal("10.00")

    def test_premium_plan_is_also_ten_percent(self):
        assert vote_platform_fee_ghs(Decimal("100.00"), "premium", None) == Decimal("10.00")

    def test_custom_plan_uses_override(self):
        assert vote_platform_fee_ghs(Decimal("100.00"), "custom", 500) == Decimal("5.00")

    def test_custom_plan_with_no_override_is_zero(self):
        assert vote_platform_fee_ghs(Decimal("100.00"), "custom", None) == Decimal("0.00")

    def test_buyer_absorbed_fee_is_backed_out_not_taxed_again(self):
        # Buyer paid GH₵110 total = GH₵100 base + GH₵10 (10%) surcharge.
        # The fee is the GH₵10 that was added, not 10% of the GH₵110 total.
        assert vote_platform_fee_ghs(Decimal("110.00"), "standard", None, fee_payer="buyer") == Decimal("10.00")


class TestTicketPlatformFee:
    def test_standard_plan_is_eight_percent(self):
        assert ticket_platform_fee_ghs(Decimal("100.00"), "standard", None) == Decimal("8.00")

    def test_premium_plan_is_also_eight_percent(self):
        assert ticket_platform_fee_ghs(Decimal("100.00"), "premium", None) == Decimal("8.00")

    def test_custom_plan_uses_override(self):
        assert ticket_platform_fee_ghs(Decimal("100.00"), "custom", 500) == Decimal("5.00")

    def test_custom_plan_with_no_override_is_zero(self):
        assert ticket_platform_fee_ghs(Decimal("100.00"), "custom", None) == Decimal("0.00")


class TestBuyerFacingVotePrice:
    def test_organizer_absorbs_by_default(self):
        assert buyer_facing_vote_price_pesewas(1000, "standard", None, "organizer") == 1000

    def test_buyer_pays_surcharge(self):
        # 1000 pesewas base, 10% standard commission = 100 pesewas fee on top.
        assert buyer_facing_vote_price_pesewas(1000, "standard", None, "buyer") == 1100

    def test_custom_plan_override_applies_to_surcharge(self):
        assert buyer_facing_vote_price_pesewas(1000, "custom", 500, "buyer") == 1050


class TestBuyerFacingTicketPrice:
    def test_organizer_absorbs_by_default(self):
        assert buyer_facing_ticket_price_ghs(Decimal("100.00"), "standard", None, "organizer") == Decimal("100.00")

    def test_buyer_pays_surcharge(self):
        # 8% standard ticket commission on GH₵100 = GH₵8.00 on top.
        assert buyer_facing_ticket_price_ghs(Decimal("100.00"), "standard", None, "buyer") == Decimal("108.00")


class TestProcessingFee:
    def test_uses_real_paystack_fee_when_present(self):
        # Paystack reports fees in the smallest currency unit (pesewas).
        assert processing_fee_ghs({"fees": 250}, Decimal("100.00")) == Decimal("2.50")

    def test_falls_back_to_estimate_when_missing(self):
        fee = processing_fee_ghs(None, Decimal("100.00"))
        assert fee == Decimal("1.95")

    def test_falls_back_when_fees_key_absent(self):
        fee = processing_fee_ghs({"channel": "card"}, Decimal("100.00"))
        assert fee == Decimal("1.95")

    def test_falls_back_on_unparseable_fees(self):
        fee = processing_fee_ghs({"fees": "not-a-number"}, Decimal("100.00"))
        assert fee == Decimal("1.95")


class TestExtractChannel:
    def test_card(self):
        assert extract_channel({"channel": "card"}) == "Card"

    def test_mobile_money(self):
        assert extract_channel({"channel": "mobile_money"}) == "Mobile Money"

    def test_bank_variants(self):
        for value in ("bank", "bank_transfer", "eft"):
            assert extract_channel({"channel": value}) == "Bank"

    def test_unrecognized_channel_is_other(self):
        assert extract_channel({"channel": "ussd"}) == "Other"

    def test_missing_response_is_unknown(self):
        assert extract_channel(None) == "Unknown"

    def test_empty_channel_is_unknown(self):
        assert extract_channel({}) == "Unknown"
