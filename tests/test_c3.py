"""C3 -- batch tier adoption, with every figure hand-calculated.

Batch is a flat 50% discount on both input and output, and it stacks with the
caching multipliers, so it applies to all five token buckets. The ceiling is
therefore exactly half of standard-tier spend.
"""

from datetime import datetime, timezone
from decimal import Decimal

from claude_spend_audit.checks import CheckConfig, run_checks
from claude_spend_audit.checks.c3_batch_tier import CHECK as C3
from claude_spend_audit.findings import Confidence
from claude_spend_audit.frame import Frame, Segment, TokenBuckets, UsageRow
from claude_spend_audit.gaps import GapCode
from claude_spend_audit.ingest import load_usage_csv

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 2, tzinfo=timezone.utc)

GROUPED = frozenset({"model", "workspace_id", "service_tier", "speed"})


def seg(**kw) -> Segment:
    base = dict(
        model="claude-opus-5",
        workspace_id="wrkspc_test",
        service_tier="standard",
        inference_geo="global",
        speed="standard",
    )
    return Segment(**(base | kw))


def frame_of(*rows: tuple[Segment, TokenBuckets]) -> Frame:
    return Frame(
        usage=tuple(
            UsageRow(start=T0, end=T1, segment=s, tokens=t) for s, t in rows
        ),
        grouped_by=GROUPED,
        bucket_width="1d",
    )


def only(findings):
    assert len(findings) == 1, f"expected one finding, got {len(findings)}"
    return findings[0]


# --------------------------------------------------------------------------
# The ceiling
# --------------------------------------------------------------------------


def test_ceiling_is_half_of_standard_spend(pricing):
    """claude-opus-5, standard tier:

        uncached 10,000,000 @ $5.00/MTok  = $50.00
        output    2,000,000 @ $25.00/MTok = $50.00
                                            -------
        standard spend                      $100.00
        at batch (0.5x)                      $50.00
        ceiling = the $50.00 difference
    """
    tokens = TokenBuckets(uncached_input=10_000_000, output=2_000_000)
    finding = only(list(C3.run(frame_of((seg(), tokens)), pricing, CheckConfig())))
    assert finding.check_id == "C3"
    assert finding.measured_usd == Decimal("50.00")
    assert finding.confidence is Confidence.CANDIDATE
    assert "$100.00" in finding.summary


def test_the_discount_comes_from_the_pricing_file(pricing_data):
    """A check never contains a rate. Change the batch multiplier in the
    file to 0.6 and the ceiling follows -- to 40%, not 50%.

        $100.00 standard spend (as above) x (1 - 0.6) = $40.00 ceiling
        each segment's batch-rate column is $100.00 x 0.6 = $60.00
    """
    from claude_spend_audit.pricing import PricingTable

    pricing_data["service_tier_multipliers"]["batch"]["value"] = "0.6"
    edited = PricingTable(pricing_data)
    tokens = TokenBuckets(uncached_input=10_000_000, output=2_000_000)
    finding = only(list(C3.run(frame_of((seg(), tokens)), edited, CheckConfig())))
    assert finding.measured_usd == Decimal("40.00")
    assert "40%" in finding.title
    assert finding.evidence.rows[0][2] == "$60.00"


def test_discount_applies_to_cache_buckets_too(pricing):
    """Batch stacks with the caching multipliers, so all five buckets halve.

        uncached  1,000,000 @  $5.00 =  $5.00
        read      2,000,000 @  $0.50 =  $1.00
        5m write    400,000 @  $6.25 =  $2.50
        1h write    100,000 @ $10.00 =  $1.00
        output      200,000 @ $25.00 =  $5.00
                                        ------
                                        $14.50   -> ceiling $7.25
    """
    tokens = TokenBuckets(
        uncached_input=1_000_000,
        cache_read=2_000_000,
        cache_write_5m=400_000,
        cache_write_1h=100_000,
        output=200_000,
    )
    finding = only(list(C3.run(frame_of((seg(), tokens)), pricing, CheckConfig())))
    assert finding.measured_usd == Decimal("7.25")


# --------------------------------------------------------------------------
# Fast mode -- the exclusion that keeps the number honest
# --------------------------------------------------------------------------


def test_fast_mode_is_excluded(pricing):
    """The Batch API does not accept fast mode, so quoting a discount on it
    would be quoting one that cannot be taken.

    Standard-speed segment: 10 MTok uncached @ $5.00 = $50.00 -> ceiling $25.00
    Fast segment at $10.00/MTok = $100.00, excluded entirely.
    """
    tokens = TokenBuckets(uncached_input=10_000_000)
    finding = only(
        list(
            C3.run(
                frame_of((seg(), tokens), (seg(speed="fast"), tokens)),
                pricing,
                CheckConfig(),
            )
        )
    )
    assert finding.measured_usd == Decimal("25.00")
    assert "$100.00 of fast-mode spend is excluded" in finding.evidence.note
    assert any("not available for fast mode" in c for c in finding.caveats)


def test_batch_tier_spend_is_not_counted(pricing):
    """Spend already on batch has nothing left to save."""
    tokens = TokenBuckets(uncached_input=10_000_000)
    assert list(C3.run(frame_of((seg(service_tier="batch"), tokens)), pricing, CheckConfig())) == []


def test_priority_tier_is_not_counted(pricing):
    """Unpriceable, so it contributes no spend to halve."""
    tokens = TokenBuckets(uncached_input=10_000_000)
    assert (
        list(C3.run(frame_of((seg(service_tier="priority"), tokens)), pricing, CheckConfig()))
        == []
    )


# --------------------------------------------------------------------------
# The range, and the candidate rule
# --------------------------------------------------------------------------


def test_floor_is_zero_by_default(pricing):
    """You cannot tell an async pipeline from an interactive request in
    aggregate data, so zero eligible is a real outcome."""
    tokens = TokenBuckets(uncached_input=10_000_000)
    finding = only(list(C3.run(frame_of((seg(), tokens)), pricing, CheckConfig())))
    assert finding.recoverable.low == Decimal("0")
    assert finding.recoverable.high == Decimal("25.00")
    assert any("tolerates asynchronous processing" in a for a in finding.recoverable.assumptions)


def test_eligible_share_narrows_the_floor(pricing):
    """--batch-eligible-share 0.5 -> floor = 0.5 x $25.00 = $12.50"""
    tokens = TokenBuckets(uncached_input=10_000_000)
    config = CheckConfig(batch_eligible_share=Decimal("0.5"))
    finding = only(list(C3.run(frame_of((seg(), tokens)), pricing, config)))
    assert finding.recoverable.low == Decimal("12.50")
    assert any("your estimate, not a measurement" in a for a in finding.recoverable.assumptions)


# --------------------------------------------------------------------------
# Preconditions
# --------------------------------------------------------------------------


def test_ungrouped_service_tier_blocks_the_check(pricing, usage_profile, tmp_path):
    """Without service_tier, assuming everything is standard would overstate
    the prize. The registry raises a gap naming C3 instead."""
    path = tmp_path / "no_tier.csv"
    path.write_text(
        "start,model,workspace_id,uncached_input_tokens,output_tokens\n"
        "2026-08-01T00:00:00Z,claude-opus-5,w,10000000,2000000\n",
        encoding="utf-8",
    )
    frame, _ = load_usage_csv(path, usage_profile)
    assert not frame.has_dimension("service_tier")

    unmet = C3.unmet(frame)
    assert len(unmet) == 1
    assert unmet[0].code is GapCode.DIMENSION_NOT_GROUPED
    assert "Batch tier adoption (C3)" in unmet[0].message

    run = run_checks(frame, pricing, CheckConfig())
    assert not [f for f in run.findings if f.check_id == "C3"]


def test_against_the_fixture(pricing, usage_profile, usage_csv):
    """The shipped fixture has two standard-tier opus segments.

        $14.50 (day 1, global) + $8.25 (day 2, us) = $22.75
        ceiling = $11.38 (half, rounded to cents by the renderer)
    """
    frame, _ = load_usage_csv(usage_csv, usage_profile)
    finding = only(list(C3.run(frame, pricing, CheckConfig())))
    assert finding.measured_usd == Decimal("22.75") / 2
    # Both segments share a model/workspace label, so they roll into one row.
    assert len(finding.evidence.rows) == 1
    assert finding.evidence.rows[0][1] == "$22.75"


def test_the_floor_clause_matches_the_actual_floor(pricing):
    """As with C2: once a share is supplied the floor is not $0, and the
    assumption text must not keep claiming it is."""
    tokens = TokenBuckets(uncached_input=10_000_000)

    default = only(list(C3.run(frame_of((seg(), tokens)), pricing, CheckConfig())))
    assert any("floor of $0" in a for a in default.recoverable.assumptions)

    narrowed = only(
        list(
            C3.run(
                frame_of((seg(), tokens)),
                pricing,
                CheckConfig(batch_eligible_share=Decimal("0.5")),
            )
        )
    )
    assert narrowed.recoverable.low == Decimal("12.50")
    assert not any("floor of $0" in a for a in narrowed.recoverable.assumptions)
