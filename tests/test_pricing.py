"""Pricing arithmetic, with every expected figure hand-calculated in a comment.

Every check has a golden-file test with a synthetic fixture where the expected
dollar figure is hand-calculated in a comment. When a formula changes, the
test catches it.

Rates used below, all from pricing/2026-09.json:

    claude-opus-5     input $5     5m write $6.25   1h write $10   read $0.50   output $25
    claude-sonnet-5   input $2     5m write $2.50   1h write $4    read $0.20   output $10
    claude-haiku-4-5  input $1     5m write $1.25   1h write $2    read $0.10   output $5
"""

from decimal import Decimal

import pytest

from claude_spend_audit.frame import Segment, TokenBuckets
from claude_spend_audit.gaps import GapCode
from claude_spend_audit.pricing import Priced, Unpriced

MTOK = 1_000_000


def cost(pricing, model, bucket, tokens, **dims) -> Decimal:
    rate = pricing.rate_for(model, bucket, **dims)
    assert isinstance(rate, Priced), f"expected a rate, got {rate}"
    return rate.cost_of(tokens)


# --------------------------------------------------------------------------
# Base rates
# --------------------------------------------------------------------------


def test_uncached_input(pricing):
    # 1,000,000 uncached input on claude-opus-5 @ $5.00/MTok = $5.00
    assert cost(pricing, "claude-opus-5", "uncached_input", MTOK) == Decimal("5.00")


def test_output(pricing):
    # 1,000,000 output on claude-opus-5 @ $25.00/MTok = $25.00
    assert cost(pricing, "claude-opus-5", "output", MTOK) == Decimal("25.00")


def test_cache_read(pricing):
    # 1,000,000 cache reads on claude-opus-5 @ $0.50/MTok = $0.50
    assert cost(pricing, "claude-opus-5", "cache_read", MTOK) == Decimal("0.50")


def test_cache_write_5m(pricing):
    # 1,000,000 5m cache writes on claude-opus-5 @ $6.25/MTok = $6.25
    # (1.25x the $5.00 base input rate)
    assert cost(pricing, "claude-opus-5", "cache_write_5m", MTOK) == Decimal("6.25")


def test_cache_write_1h(pricing):
    # 1,000,000 1h cache writes on claude-opus-5 @ $10.00/MTok = $10.00
    # (2x the $5.00 base input rate)
    assert cost(pricing, "claude-opus-5", "cache_write_1h", MTOK) == Decimal("10.00")


def test_the_two_cache_ttls_are_not_the_same_price(pricing):
    """The delta that C1 depends on, and that a 4-bucket token model loses.

    1h writes cost 1.6x what 5m writes cost ($10.00 vs $6.25), so break-even is
    2 cache reads for 1h against 1 for 5m. Summing the two buckets would make
    this arithmetic impossible to state.
    """
    five_m = cost(pricing, "claude-opus-5", "cache_write_5m", MTOK)
    one_h = cost(pricing, "claude-opus-5", "cache_write_1h", MTOK)
    assert one_h > five_m
    assert one_h / five_m == Decimal("1.6")


def test_cache_write_breakeven_is_per_ttl(pricing):
    """Writing then reading once: profitable at 5m, a loss at 1h.

    Baseline, no caching at all: 1,000,000 uncached input twice
        2 x $5.00 = $10.00

    5m cache, write once then read once:
        write  1,000,000 @ $6.25/MTok = $6.25
        read   1,000,000 @ $0.50/MTok = $0.50
        total                           $6.75   -> saves $3.25

    1h cache, write once then read once:
        write  1,000,000 @ $10.00/MTok = $10.00
        read   1,000,000 @  $0.50/MTok =  $0.50
        total                            $10.50  -> LOSES $0.50

    A tool that summed the cache_creation buckets could not tell these apart.
    """
    uncached_twice = cost(pricing, "claude-opus-5", "uncached_input", MTOK) * 2
    assert uncached_twice == Decimal("10.00")

    five_m_total = cost(pricing, "claude-opus-5", "cache_write_5m", MTOK) + cost(
        pricing, "claude-opus-5", "cache_read", MTOK
    )
    assert five_m_total == Decimal("6.75")
    assert five_m_total < uncached_twice  # worth it after one read

    one_h_total = cost(pricing, "claude-opus-5", "cache_write_1h", MTOK) + cost(
        pricing, "claude-opus-5", "cache_read", MTOK
    )
    assert one_h_total == Decimal("10.50")
    assert one_h_total > uncached_twice  # still under water after one read


# --------------------------------------------------------------------------
# Multipliers
# --------------------------------------------------------------------------


def test_batch_tier_halves_input(pricing):
    # 1,000,000 uncached input on claude-opus-5, batch tier
    #   $5.00/MTok x 0.5 = $2.50/MTok -> $2.50
    # Cross-check: the published batch table lists Opus 5 at $2.50/MTok input.
    assert cost(
        pricing, "claude-opus-5", "uncached_input", MTOK, service_tier="batch"
    ) == Decimal("2.50")


def test_batch_tier_halves_output(pricing):
    # 1,000,000 output on claude-opus-5, batch tier
    #   $25.00/MTok x 0.5 = $12.50/MTok -> $12.50
    # Cross-check: published batch table lists Opus 5 at $12.50/MTok output.
    assert cost(
        pricing, "claude-opus-5", "output", MTOK, service_tier="batch"
    ) == Decimal("12.50")


def test_us_inference_geo_adds_ten_percent(pricing):
    # 1,000,000 uncached input on claude-opus-5, inference_geo=us
    #   $5.00/MTok x 1.1 = $5.50/MTok -> $5.50
    assert cost(
        pricing, "claude-opus-5", "uncached_input", MTOK, inference_geo="us"
    ) == Decimal("5.50")


def test_global_inference_geo_is_unchanged(pricing):
    # Global routing is the default and carries no multiplier.
    assert cost(
        pricing, "claude-opus-5", "uncached_input", MTOK, inference_geo="global"
    ) == Decimal("5.00")


def test_multipliers_stack(pricing):
    # 1,000,000 output on claude-opus-5, batch tier AND inference_geo=us
    #   $25.00 x 0.5 x 1.1 = $13.75/MTok -> $13.75
    assert cost(
        pricing,
        "claude-opus-5",
        "output",
        MTOK,
        service_tier="batch",
        inference_geo="us",
    ) == Decimal("13.75")


def test_fast_mode_replaces_base_rates(pricing):
    # Fast mode reprices Opus 5 to $10/$50 per MTok.
    #   1,000,000 uncached input @ $10.00/MTok = $10.00
    #   1,000,000 output         @ $50.00/MTok = $50.00
    assert cost(
        pricing, "claude-opus-5", "uncached_input", MTOK, speed="fast"
    ) == Decimal("10.00")
    assert cost(pricing, "claude-opus-5", "output", MTOK, speed="fast") == Decimal(
        "50.00"
    )


def test_fast_mode_cache_multipliers_stack_on_fast_base(pricing):
    # Caching multipliers apply on top of fast mode pricing, so on a $10 base:
    #   5m write  1.25 x $10 = $12.50/MTok
    #   1h write     2 x $10 = $20.00/MTok
    #   read       0.1 x $10 =  $1.00/MTok
    assert cost(
        pricing, "claude-opus-5", "cache_write_5m", MTOK, speed="fast"
    ) == Decimal("12.50")
    assert cost(
        pricing, "claude-opus-5", "cache_write_1h", MTOK, speed="fast"
    ) == Decimal("20.00")
    assert cost(pricing, "claude-opus-5", "cache_read", MTOK, speed="fast") == Decimal(
        "1.00"
    )


def test_fast_mode_falls_back_for_models_without_an_override(pricing):
    # Fast mode is Opus 5 / 4.8 only. Sonnet 5 keeps standard rates rather than
    # being priced at some invented premium.
    assert cost(
        pricing, "claude-sonnet-5", "uncached_input", MTOK, speed="fast"
    ) == Decimal("2.00")


# --------------------------------------------------------------------------
# Refusing to price -- the property that keeps totals honest
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tier", ["priority", "priority_on_demand", "flex", "flex_discount"]
)
def test_unpriceable_tiers_are_unpriced_not_zero(pricing, tier):
    """A tier with no published rate must never silently price at $0.

    It raises a loud warning rather than a wrong total.
    """
    rate = pricing.rate_for("claude-opus-5", "uncached_input", service_tier=tier)
    assert isinstance(rate, Unpriced)
    assert rate.cost_of(MTOK) is None
    assert rate.gap.invalidates_total is True


def test_priority_tier_raises_the_priority_gap_specifically(pricing):
    rate = pricing.rate_for("claude-opus-5", "output", service_tier="priority")
    assert isinstance(rate, Unpriced)
    assert rate.gap.code is GapCode.PRIORITY_TIER_PRESENT


def test_unknown_model_is_unpriced_not_guessed(pricing):
    rate = pricing.rate_for("claude-imaginary-9", "output")
    assert isinstance(rate, Unpriced)
    assert rate.gap.code is GapCode.UNKNOWN_MODEL
    assert "claude-imaginary-9" in rate.gap.message


def test_no_model_at_all_is_unpriced(pricing):
    assert isinstance(pricing.rate_for(None, "output"), Unpriced)


# --------------------------------------------------------------------------
# Model resolution -- exact matches only
# --------------------------------------------------------------------------


def test_display_name_resolves(pricing):
    # A Console CSV is likely to say "Claude Opus 5", not "claude-opus-5".
    assert pricing.resolve_model("Claude Opus 5") == "claude-opus-5"
    assert pricing.resolve_model("claude opus 5") == "claude-opus-5"


def test_dated_snapshot_resolves_via_the_alias_table(pricing):
    assert pricing.resolve_model("claude-haiku-4-5-20251001") == "claude-haiku-4-5"


def test_near_miss_does_not_resolve(pricing):
    """No fuzzy matching. Pricing the wrong model beats no price only if you
    do not mind being wrong, and this tool minds."""
    assert pricing.resolve_model("claude-opus-5-turbo") is None
    assert pricing.resolve_model("opus") is None


# --------------------------------------------------------------------------
# Whole-segment estimation
# --------------------------------------------------------------------------


def test_estimate_of_a_full_segment(pricing):
    """The first fixture row, priced bucket by bucket.

        uncached      1,000,000 @  $5.00/MTok =  $5.00
        cache read    2,000,000 @  $0.50/MTok =  $1.00
        5m write        400,000 @  $6.25/MTok =  $2.50
        1h write        100,000 @ $10.00/MTok =  $1.00
        output          200,000 @ $25.00/MTok =  $5.00
                                                ------
                                                $14.50
    """
    estimate = pricing.estimate_tokens(
        TokenBuckets(
            uncached_input=1_000_000,
            cache_read=2_000_000,
            cache_write_5m=400_000,
            cache_write_1h=100_000,
            output=200_000,
        ),
        Segment(
            model="claude-opus-5",
            service_tier="standard",
            inference_geo="global",
            speed="standard",
        ),
    )
    assert estimate.usd == Decimal("14.50")
    assert estimate.is_complete
    assert estimate.priced_tokens == 3_700_000


def test_estimate_excludes_unpriceable_tokens_rather_than_zeroing_them(pricing):
    """A priority-tier segment contributes tokens but no dollars.

        uncached 5,000,000 + output 1,000,000 = 6,000,000 tokens, $0 priced.

    The tokens still show in the count so the report can say how much of the
    bill it could not see.
    """
    estimate = pricing.estimate_tokens(
        TokenBuckets(uncached_input=5_000_000, output=1_000_000),
        Segment(model="claude-haiku-4-5", service_tier="priority"),
    )
    assert estimate.usd == Decimal("0")
    assert estimate.unpriced_tokens == 6_000_000
    assert estimate.priced_tokens == 0
    assert not estimate.is_complete
    assert any(g.code is GapCode.PRIORITY_TIER_PRESENT for g in estimate.gaps)


def test_web_search_rate(pricing):
    # $10 per 1,000 searches -> 50 searches = $0.50
    assert pricing.web_search_rate() * Decimal(50) / Decimal("1000") == Decimal("0.50")


def test_model_tiers_are_available_for_c8(pricing):
    assert pricing.tier_of("claude-opus-5") == "frontier"
    assert pricing.tier_of("claude-sonnet-5") == "mid"
    assert pricing.tier_of("claude-haiku-4-5") == "small"
    assert pricing.tier_of("claude-imaginary-9") is None


def test_every_model_on_file_has_a_known_tier(pricing):
    """C8 groups by tier. A model with a typo'd or missing tier would land in
    the 'unclassified' row -- correct behaviour for a user's edited file, but
    the shipped file should never need it."""
    for model_id in pricing.model_ids:
        assert pricing.tier_of(model_id) in {"frontier", "mid", "small"}, model_id


def test_the_tier_classification_says_it_is_not_published(pricing_data):
    """Every other value in the file names its source. The tiers have none,
    because they are a judgement -- and the file has to say so."""
    note = pricing_data["model_tiers"]
    assert note["provenance"] == "classification"
    assert "not Anthropic's" in note["note"]


def test_tier_of_resolves_display_names_and_aliases(pricing):
    assert pricing.tier_of("Claude Opus 5") == "frontier"
    assert pricing.tier_of("claude-haiku-4-5-20251001") == "small"


# --------------------------------------------------------------------------
# Long context
# --------------------------------------------------------------------------


def test_long_context_models_all_exist_in_the_rate_card(pricing):
    """A typo in the confirmation list would silently un-confirm a model."""
    assert pricing.long_context_models
    assert pricing.long_context_models <= set(pricing.model_ids)


@pytest.mark.parametrize(
    "model",
    ["claude-opus-5", "claude-sonnet-5", "claude-opus-4-6", "claude-sonnet-4-6",
     "claude-fable-5-1", "Claude Opus 5"],
)
def test_long_context_is_confirmed_for_4_6_and_later(pricing, model):
    # Published: "Claude 4.6 and later models ... include the full 1M token
    # context window at standard pricing."
    assert pricing.long_context_confirmed(model)


@pytest.mark.parametrize(
    "model", ["claude-sonnet-4-5", "claude-sonnet-4", "claude-opus-4-5", "claude-opus-4-1"]
)
def test_long_context_is_not_confirmed_for_earlier_models(pricing, model):
    """The page is silent about these. Silence is not a confirmation."""
    assert not pricing.long_context_confirmed(model)


def test_an_unknown_model_is_not_confirmed(pricing):
    assert not pricing.long_context_confirmed("claude-imaginary-9")
    assert not pricing.long_context_confirmed(None)


def test_long_context_does_not_change_the_rate(pricing):
    """No premium: the same tokens cost the same in either window.

        1,000,000 uncached on claude-opus-5 = $5.00 at 9k context or 900k.
    """
    tokens = TokenBuckets(uncached_input=MTOK)
    short = pricing.estimate_tokens(tokens, Segment(model="claude-opus-5", context_window="0-200k"))
    long = pricing.estimate_tokens(tokens, Segment(model="claude-opus-5", context_window="200k-1M"))
    assert short.usd == long.usd == Decimal("5.00")


# --------------------------------------------------------------------------
# Per-bucket costs
# --------------------------------------------------------------------------


def test_bucket_costs_break_out_the_estimate(pricing):
    """The same fixture row as test_estimate_of_a_full_segment, by bucket.
    The parts must sum to the whole, or C4's composition would not reconcile."""
    tokens = TokenBuckets(
        uncached_input=1_000_000,
        cache_read=2_000_000,
        cache_write_5m=400_000,
        cache_write_1h=100_000,
        output=200_000,
    )
    segment = Segment(model="claude-opus-5", service_tier="standard")
    costs = pricing.bucket_costs(tokens, segment)
    assert costs == {
        "uncached_input": Decimal("5.00"),
        "cache_read": Decimal("1.00"),
        "cache_write_5m": Decimal("2.50"),
        "cache_write_1h": Decimal("1.00"),
        "output": Decimal("5.00"),
    }
    assert sum(costs.values()) == pricing.estimate_tokens(tokens, segment).usd


def test_bucket_costs_omit_unpriced_buckets_rather_than_zeroing_them(pricing):
    costs = pricing.bucket_costs(
        TokenBuckets(uncached_input=MTOK, output=MTOK),
        Segment(model="claude-haiku-4-5", service_tier="priority"),
    )
    assert costs == {}


def test_bucket_costs_skip_empty_buckets(pricing):
    costs = pricing.bucket_costs(
        TokenBuckets(uncached_input=MTOK), Segment(model="claude-opus-5")
    )
    assert list(costs) == ["uncached_input"]


# --------------------------------------------------------------------------
# Service tier multipliers
# --------------------------------------------------------------------------


def test_service_tier_multiplier_reads_the_file(pricing):
    assert pricing.service_tier_multiplier("batch") == Decimal("0.5")
    assert pricing.service_tier_multiplier("standard") == Decimal("1")


def test_a_missing_service_tier_multiplier_is_an_error_not_a_default(pricing):
    from claude_spend_audit.pricing import PricingError

    with pytest.raises(PricingError, match="priority"):
        pricing.service_tier_multiplier("priority")
