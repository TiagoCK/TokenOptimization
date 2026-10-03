"""C2 -- cache adoption gap, with every figure hand-calculated.

claude-opus-5 rates, from pricing/2026-09.json, in $/MTok:
    base input $5.00   cache read $0.50   5m write $6.25   1h write $10.00
"""

from datetime import datetime, timezone
from decimal import Decimal

from claude_spend_audit.checks import CheckConfig
from claude_spend_audit.checks.c2_cache_adoption import CHECK as C2, _hit_rate
from claude_spend_audit.findings import Confidence
from claude_spend_audit.frame import Frame, Segment, TokenBuckets, UsageRow

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 2, tzinfo=timezone.utc)

OPUS = Segment(
    model="claude-opus-5",
    workspace_id="wrkspc_test",
    service_tier="standard",
    inference_geo="global",
    speed="standard",
)


def frame_of(tokens: TokenBuckets, segment: Segment = OPUS) -> Frame:
    return Frame(
        usage=(UsageRow(start=T0, end=T1, segment=segment, tokens=tokens),),
        grouped_by=frozenset({"model", "workspace_id", "service_tier"}),
        bucket_width="1d",
    )


def only(findings):
    assert len(findings) == 1, f"expected one finding, got {len(findings)}"
    return findings[0]


# --------------------------------------------------------------------------
# The hit rate, by the standard formula
# --------------------------------------------------------------------------


def test_hit_rate_formula():
    """cache_read / (cache_read + uncached_input + cache_creation)

        200,000 / (200,000 + 700,000 + 100,000) = 0.20
    """
    tokens = TokenBuckets(
        cache_read=200_000, uncached_input=700_000, cache_write_5m=100_000
    )
    assert _hit_rate(tokens) == Decimal("0.2")


def test_hit_rate_of_zero_when_nothing_is_cached():
    assert _hit_rate(TokenBuckets(uncached_input=1_000_000)) == Decimal("0")


def test_hit_rate_ignores_output():
    """Output tokens are not part of the denominator -- they can never be
    cached, so including them would understate the gap."""
    tokens = TokenBuckets(cache_read=500_000, uncached_input=500_000, output=9_000_000)
    assert _hit_rate(tokens) == Decimal("0.5")


# --------------------------------------------------------------------------
# The ceiling
# --------------------------------------------------------------------------


def test_ceiling_is_uncached_times_base_minus_read(pricing):
    """10,000,000 uncached input on claude-opus-5, no cache reads.

        base - read = $5.00 - $0.50 = $4.50/MTok
        10 MTok x $4.50 = $45.00
    """
    finding = only(
        list(C2.run(frame_of(TokenBuckets(uncached_input=10_000_000)), pricing, CheckConfig()))
    )
    assert finding.check_id == "C2"
    assert finding.measured_usd == Decimal("45.00")
    assert finding.confidence is Confidence.CANDIDATE


def test_ceiling_is_labelled_as_unreachable(pricing):
    """It is a bound, not an expectation: at least one cache write must be
    paid, so `U*(base-read)` is never actually achieved."""
    finding = only(
        list(C2.run(frame_of(TokenBuckets(uncached_input=10_000_000)), pricing, CheckConfig()))
    )
    assert "upper bound" in finding.measured_basis
    assert any("cannot actually be reached" in a for a in finding.recoverable.assumptions)


def test_ceiling_scales_with_the_model(pricing):
    """Sonnet 5: base $2.00 - read $0.20 = $1.80/MTok. 10 MTok -> $18.00"""
    sonnet = Segment(
        model="claude-sonnet-5", service_tier="standard", inference_geo="global"
    )
    finding = only(
        list(
            C2.run(
                frame_of(TokenBuckets(uncached_input=10_000_000), sonnet),
                pricing,
                CheckConfig(),
            )
        )
    )
    assert finding.measured_usd == Decimal("18.00")


# --------------------------------------------------------------------------
# The range, and the candidate rule
# --------------------------------------------------------------------------


def test_floor_is_zero_by_default(pricing):
    """A prompt with no stable prefix cannot be cached at all, so $0 is a real
    outcome. Anything above zero would be an invented percentage."""
    finding = only(
        list(C2.run(frame_of(TokenBuckets(uncached_input=10_000_000)), pricing, CheckConfig()))
    )
    assert finding.recoverable.low == Decimal("0")
    assert finding.recoverable.high == Decimal("45.00")
    assert not finding.recoverable.is_point


def test_prefix_share_narrows_the_floor(pricing):
    """--cache-prefix-share 0.4 -> floor = 0.4 x $45.00 = $18.00"""
    config = CheckConfig(cache_prefix_share=Decimal("0.4"))
    finding = only(
        list(C2.run(frame_of(TokenBuckets(uncached_input=10_000_000)), pricing, config))
    )
    assert finding.recoverable.low == Decimal("18.00")
    assert finding.recoverable.high == Decimal("45.00")
    # And it says whose estimate that is.
    assert any("your estimate, not a measurement" in a for a in finding.recoverable.assumptions)


def test_assumptions_point_at_c1(pricing):
    """Acting on C2 without reading C1 is how a cache-adoption win becomes a
    cache-amortization loss. The finding says so."""
    finding = only(
        list(C2.run(frame_of(TokenBuckets(uncached_input=10_000_000)), pricing, CheckConfig()))
    )
    assert any("C1" in a for a in finding.recoverable.assumptions)


# --------------------------------------------------------------------------
# Staying quiet
# --------------------------------------------------------------------------


def test_a_well_cached_segment_produces_nothing(pricing):
    """80% hit rate is not a cache adoption gap.

        8,000,000 / (8,000,000 + 2,000,000) = 0.80, well above the 0.20 bar.
    """
    tokens = TokenBuckets(cache_read=8_000_000, uncached_input=2_000_000)
    assert list(C2.run(frame_of(tokens), pricing, CheckConfig())) == []


def test_a_segment_with_no_uncached_input_produces_nothing(pricing):
    tokens = TokenBuckets(cache_read=5_000_000, output=1_000_000)
    assert list(C2.run(frame_of(tokens), pricing, CheckConfig())) == []


def test_below_the_floor_produces_nothing(pricing):
    """100,000 uncached x $4.50/MTok = $0.45, under the $1 default."""
    tokens = TokenBuckets(uncached_input=100_000)
    assert list(C2.run(frame_of(tokens), pricing, CheckConfig())) == []


def test_unpriceable_segment_is_skipped(pricing):
    priority = Segment(model="claude-opus-5", service_tier="priority")
    tokens = TokenBuckets(uncached_input=10_000_000)
    assert list(C2.run(frame_of(tokens, priority), pricing, CheckConfig())) == []


def test_evidence_rolls_up_by_display_label(pricing):
    """Two segments differing only by geo share a model/workspace label.

    They are priced separately -- the US geo carries a 1.1x multiplier -- but
    shown as one row, because two identical-looking rows read as a bug.

        global: 10 MTok x $4.50           = $45.00
        us:     10 MTok x $4.50 x 1.1     = $49.50
                                            ------
                                            $94.50
    """
    glob = Segment(model="claude-opus-5", workspace_id="w", service_tier="standard",
                   inference_geo="global")
    us = Segment(model="claude-opus-5", workspace_id="w", service_tier="standard",
                 inference_geo="us")
    tokens = TokenBuckets(uncached_input=10_000_000)
    frame = Frame(
        usage=(
            UsageRow(start=T0, end=T1, segment=glob, tokens=tokens),
            UsageRow(start=T0, end=T1, segment=us, tokens=tokens),
        ),
        grouped_by=frozenset({"model", "workspace_id", "service_tier", "inference_geo"}),
        bucket_width="1d",
    )
    finding = only(list(C2.run(frame, pricing, CheckConfig())))
    assert finding.measured_usd == Decimal("94.50")
    assert len(finding.evidence.rows) == 1
    assert finding.evidence.rows[0][1] == "20.0M"  # both segments' uncached input


def test_the_floor_clause_matches_the_actual_floor(pricing):
    """With a share set, the range no longer starts at $0 -- so the assumption
    must stop saying it does. Two contradictory statements in one block is
    exactly what a sceptical reader seizes on."""
    tokens = TokenBuckets(uncached_input=10_000_000)

    default = only(list(C2.run(frame_of(tokens), pricing, CheckConfig())))
    assert default.recoverable.low == Decimal("0")
    assert any("floor of $0" in a for a in default.recoverable.assumptions)

    narrowed = only(
        list(
            C2.run(
                frame_of(tokens), pricing, CheckConfig(cache_prefix_share=Decimal("0.4"))
            )
        )
    )
    assert narrowed.recoverable.low == Decimal("18.00")
    assert not any("floor of $0" in a for a in narrowed.recoverable.assumptions)
