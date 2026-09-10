"""C1 -- cache write amortization, with every figure hand-calculated.

Rates for claude-opus-5, from pricing/2026-09.json, in $/MTok:

    base input   $5.00
    5m write     $6.25   (1.25x base)
    1h write    $10.00   (2x base)
    cache read   $0.50   (0.1x base)
"""

from decimal import Decimal

from claude_spend_audit.checks import CheckConfig, run_checks
from claude_spend_audit.checks.c1_cache_amortization import CHECK as C1
from claude_spend_audit.findings import Confidence
from claude_spend_audit.frame import Frame, Segment, TokenBuckets, UsageRow
from claude_spend_audit.ingest import load_usage_csv

from datetime import datetime, timezone

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 2, tzinfo=timezone.utc)

OPUS = Segment(
    model="claude-opus-5",
    workspace_id="wrkspc_test",
    api_key_id="apikey_test",
    service_tier="standard",
    inference_geo="global",
    speed="standard",
)


def frame_of(tokens: TokenBuckets, segment: Segment = OPUS) -> Frame:
    return Frame(
        usage=(UsageRow(start=T0, end=T1, segment=segment, tokens=tokens),),
        grouped_by=frozenset({"model", "workspace_id", "api_key_id", "service_tier"}),
        bucket_width="1d",
    )


def only(findings):
    assert len(findings) == 1, f"expected one finding, got {len(findings)}"
    return findings[0]


# --------------------------------------------------------------------------
# The core arithmetic
# --------------------------------------------------------------------------


def test_wasteful_1h_cache(pricing):
    """The worked example.

        1h writes  2,000,000 @ $10.00/MTok = $20.00
        reads        200,000 @  $0.50/MTok =  $0.10
                                             -------
        actual                                $20.10

        uncached  2,200,000 @  $5.00/MTok  =  $11.00

        waste = $20.10 - $11.00 = $9.10
    """
    findings = list(
        C1.run(
            frame_of(TokenBuckets(cache_write_1h=2_000_000, cache_read=200_000)),
            pricing,
            CheckConfig(),
        )
    )
    finding = only(findings)
    assert finding.check_id == "C1"
    assert finding.measured_usd == Decimal("9.10")
    assert finding.confidence is Confidence.MEASURED


def test_recoverable_equals_the_waste_exactly(pricing):
    """Disabling caching pays `counterfactual` instead of `actual`.

    The saving is therefore exactly the waste -- arithmetic, not an estimate,
    which is why C1 is the one check allowed a point recoverable.
    """
    finding = only(
        list(
            C1.run(
                frame_of(TokenBuckets(cache_write_1h=2_000_000, cache_read=200_000)),
                pricing,
                CheckConfig(),
            )
        )
    )
    assert finding.recoverable is not None
    assert finding.recoverable.is_point
    assert finding.recoverable.low == Decimal("9.10")
    assert finding.recoverable.assumptions  # the latency trade is stated


def test_break_even_read_shortfall_is_reported(pricing):
    """Each extra read token closes the gap by (base - read) = $4.50/MTok.

        $9.10 / $4.50 per MTok = 2.0222... MTok = 2,022,222 tokens
    """
    finding = only(
        list(
            C1.run(
                frame_of(TokenBuckets(cache_write_1h=2_000_000, cache_read=200_000)),
                pricing,
                CheckConfig(),
            )
        )
    )
    assert "2,022,222" in finding.remedy


def test_ttl_switch_range_is_bounded_both_ways(pricing):
    """Moving the same traffic to a 5m TTL, both bounds.

        best  -- the reads still land:
                 2,000,000 @ $6.25 = $12.50, + $0.10 reads = $12.60
                 vs $11.00 uncached -> waste $1.60
                 saving = $9.10 - $1.60 = $7.50

        worst -- the shorter TTL expires first and no reads land:
                 2,000,000 @ $6.25 = $12.50 vs $10.00 uncached -> waste $2.50
                 saving = $9.10 - $2.50 = $6.60
    """
    finding = only(
        list(
            C1.run(
                frame_of(TokenBuckets(cache_write_1h=2_000_000, cache_read=200_000)),
                pricing,
                CheckConfig(),
            )
        )
    )
    assert "$6.60 to $7.50" in finding.remedy


def test_the_two_ttls_are_not_interchangeable(pricing):
    """Identical token counts, different TTL, opposite verdicts.

    2,000,000 writes against 200,000 reads:
        at 1h: $20.10 actual vs $11.00 uncached -> WASTES $9.10
        at 5m: $12.60 actual vs $11.00 uncached -> WASTES  $1.60

    This is the whole reason the five-bucket split exists. A summed
    cache_creation bucket would give one answer for both.
    """
    at_1h = list(
        C1.run(
            frame_of(TokenBuckets(cache_write_1h=2_000_000, cache_read=200_000)),
            pricing,
            CheckConfig(),
        )
    )
    at_5m = list(
        C1.run(
            frame_of(TokenBuckets(cache_write_5m=2_000_000, cache_read=200_000)),
            pricing,
            CheckConfig(),
        )
    )
    assert only(at_1h).measured_usd == Decimal("9.10")
    assert only(at_5m).measured_usd == Decimal("1.60")


# --------------------------------------------------------------------------
# Staying quiet -- a check that fires on healthy data is worse than useless
# --------------------------------------------------------------------------


def test_healthy_caching_produces_no_finding(pricing):
    """The M1 fixture's Opus row, which SAVES money.

        uncached  1,000,000 @  $5.00 =  $5.00
        reads     2,000,000 @  $0.50 =  $1.00
        5m write    400,000 @  $6.25 =  $2.50
        1h write    100,000 @ $10.00 =  $1.00
        cached actual = 1.00 + 2.50 + 1.00       =  $4.50
        uncached counterfactual 2,500,000 @ $5   = $12.50
        -> saves $8.00, so there is nothing to report
    """
    findings = list(
        C1.run(
            frame_of(
                TokenBuckets(
                    uncached_input=1_000_000,
                    cache_read=2_000_000,
                    cache_write_5m=400_000,
                    cache_write_1h=100_000,
                    output=200_000,
                )
            ),
            pricing,
            CheckConfig(),
        )
    )
    assert findings == []


def test_no_caching_at_all_is_not_a_c1_finding(pricing):
    """C1 is about caching that does not pay off, not caching that is absent.

    Non-adoption is C2's job, and C2 is a `candidate` for good reason.
    """
    findings = list(
        C1.run(
            frame_of(TokenBuckets(uncached_input=50_000_000, output=1_000_000)),
            pricing,
            CheckConfig(),
        )
    )
    assert findings == []


def test_waste_below_the_floor_is_dropped(pricing):
    """Sonnet 5: 1,000,000 5m writes @ $2.50 + 100,000 reads @ $0.20

        actual        = $2.50 + $0.02          = $2.52
        uncached      = 1,100,000 @ $2.00/MTok = $2.20
        waste                                  = $0.32

    Real, but not worth a row in a report that has to survive scrutiny.
    """
    sonnet = Segment(
        model="claude-sonnet-5",
        workspace_id="w",
        service_tier="standard",
        inference_geo="global",
        speed="standard",
    )
    tokens = TokenBuckets(cache_write_5m=1_000_000, cache_read=100_000)
    assert list(C1.run(frame_of(tokens, sonnet), pricing, CheckConfig())) == []
    # ... but it is found once the floor is lowered.
    lowered = list(
        C1.run(frame_of(tokens, sonnet), pricing, CheckConfig(min_impact_usd=Decimal("0.10")))
    )
    assert only(lowered).measured_usd == Decimal("0.32")


def test_unpriceable_segment_is_skipped_not_half_priced(pricing):
    """Priority tier has no published rate, so the segment yields no finding.

    M1 already raised PRIORITY_TIER_PRESENT; inventing a waste figure from a
    rate that does not exist would be worse than saying nothing.
    """
    priority = Segment(model="claude-opus-5", service_tier="priority")
    findings = list(
        C1.run(
            frame_of(TokenBuckets(cache_write_1h=2_000_000, cache_read=1_000), priority),
            pricing,
            CheckConfig(),
        )
    )
    assert findings == []


# --------------------------------------------------------------------------
# Multipliers still apply
# --------------------------------------------------------------------------


def test_us_inference_geo_scales_the_waste(pricing):
    """Every rate carries the 1.1x, so the waste scales with it.

        $9.10 x 1.1 = $10.01
    """
    us = Segment(
        model="claude-opus-5",
        service_tier="standard",
        inference_geo="us",
        speed="standard",
    )
    finding = only(
        list(
            C1.run(
                frame_of(
                    TokenBuckets(cache_write_1h=2_000_000, cache_read=200_000), us
                ),
                pricing,
                CheckConfig(),
            )
        )
    )
    assert finding.measured_usd == Decimal("10.01")


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


def test_against_the_fixture(pricing, usage_profile):
    """Only the leaky segment is reported; the healthy one is not named."""
    frame, _ = load_usage_csv("tests/fixtures/csv/usage_cache_waste.csv", usage_profile)
    run = run_checks(frame, pricing, CheckConfig())
    c1 = [f for f in run.findings if f.check_id == "C1"]
    finding = only(c1)

    assert finding.measured_usd == Decimal("9.10")
    assert finding.evidence is not None
    assert len(finding.evidence.rows) == 1
    identity = finding.evidence.rows[0][0]
    assert "wrkspc_leaky" in identity
    assert "wrkspc_healthy" not in identity


def test_segments_are_grouped_by_full_segment_not_just_model(pricing):
    """Two tiers of the same model price differently and must not be merged.

    Batch halves every rate, so a batch segment's waste is exactly half:
        $9.10 x 0.5 = $4.55
    Merging them into one 'claude-opus-5' bucket would price the combined
    tokens at whichever rate happened to be looked up.
    """
    standard = Segment(
        model="claude-opus-5", service_tier="standard", inference_geo="global"
    )
    batch = Segment(
        model="claude-opus-5", service_tier="batch", inference_geo="global"
    )
    tokens = TokenBuckets(cache_write_1h=2_000_000, cache_read=200_000)
    frame = Frame(
        usage=(
            UsageRow(start=T0, end=T1, segment=standard, tokens=tokens),
            UsageRow(start=T0, end=T1, segment=batch, tokens=tokens),
        ),
        grouped_by=frozenset({"model", "service_tier"}),
        bucket_width="1d",
    )
    finding = only(list(C1.run(frame, pricing, CheckConfig())))
    #   standard $9.10 + batch $4.55 = $13.65
    assert finding.measured_usd == Decimal("13.65")
    assert len(finding.evidence.rows) == 2
