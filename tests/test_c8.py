"""C8 -- model mix, with every figure hand-calculated.

Rates used, all from pricing/2026-09.json, standard tier:

    claude-opus-5     frontier   input $5   output $25
    claude-sonnet-5   mid        input $2   output $10
    claude-haiku-4-5  small      input $1   output $5

The golden case avoids a .5% share on purpose: Decimal formatting rounds half
to even, and shares that visibly fail to sum to 100% would read as an
arithmetic error in a report someone forwards.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from claude_spend_audit.checks import CheckConfig, run_checks
from claude_spend_audit.checks.c8_model_mix import CHECK as C8
from claude_spend_audit.findings import Confidence
from claude_spend_audit.frame import Frame, Segment, TokenBuckets, UsageRow
from claude_spend_audit.gaps import GapCode
from claude_spend_audit.pricing import PricingTable

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 2, tzinfo=timezone.utc)

GROUPED = frozenset({"model", "service_tier"})


def seg(model: str, **kw) -> Segment:
    return Segment(**({"model": model, "service_tier": "standard"} | kw))


def uncached(n: int) -> TokenBuckets:
    return TokenBuckets(uncached_input=n)


def frame_of(*rows: tuple[Segment, TokenBuckets], grouped=GROUPED) -> Frame:
    return Frame(
        usage=tuple(UsageRow(start=T0, end=T1, segment=s, tokens=t) for s, t in rows),
        grouped_by=frozenset(grouped),
        bucket_width="1d",
    )


def run(frame, pricing, **config):
    return list(C8.run(frame, pricing, CheckConfig(**config)))


def only(findings):
    assert len(findings) == 1, f"expected one finding, got {len(findings)}"
    return findings[0]


def rows_by_model(finding) -> dict[str, tuple[str, ...]]:
    return {row[0]: row for row in finding.evidence.rows}


def money(cell: str) -> Decimal:
    return Decimal(cell.lstrip("$").replace(",", ""))


#: The golden case.
#:
#:   claude-opus-5     6,000,000 uncached @ $5 = $30.00   frontier   60%
#:   claude-sonnet-5   5,000,000 uncached @ $2 = $10.00   mid        20%
#:   claude-haiku-4-5 10,000,000 uncached @ $1 = $10.00   small      20%
#:                                               ------
#:                                               $50.00
MIX = (
    (seg("claude-opus-5"), uncached(6_000_000)),
    (seg("claude-sonnet-5"), uncached(5_000_000)),
    (seg("claude-haiku-4-5"), uncached(10_000_000)),
)


def golden(pricing):
    return only(run(frame_of(*MIX), pricing))


# --------------------------------------------------------------------------
# Preconditions
# --------------------------------------------------------------------------


def test_ungrouped_model_blocks_the_check(pricing):
    frame = frame_of((Segment(service_tier="standard"), uncached(1_000_000)), grouped={"service_tier"})
    unmet = C8.unmet(frame)
    assert [g.code for g in unmet] == [GapCode.DIMENSION_NOT_GROUPED]
    assert "Model mix (C8)" in unmet[0].message
    assert not [f for f in run_checks(frame, pricing).findings if f.check_id == "C8"]


# --------------------------------------------------------------------------
# The golden figures
# --------------------------------------------------------------------------


def test_frontier_spend_is_measured(pricing):
    finding = golden(pricing)
    assert finding.check_id == "C8"
    assert finding.measured_usd == Decimal("30.00")
    assert finding.scope == "60% of priced spend on frontier models"


def test_the_mix_is_stated_tier_by_tier(pricing):
    assert (
        "60% frontier ($30.00), 20% mid ($10.00), 20% small ($10.00)"
        in golden(pricing).summary
    )


def test_one_row_per_model_frontier_first(pricing):
    assert golden(pricing).evidence.rows == (
        ("claude-opus-5", "frontier", "$30.00", "60%", "$5.00 / $25.00"),
        ("claude-sonnet-5", "mid", "$10.00", "20%", "$2.00 / $10.00"),
        ("claude-haiku-4-5", "small", "$10.00", "20%", "$1.00 / $5.00"),
    )


def test_shares_sum_to_100_and_spend_reconciles(pricing):
    finding = golden(pricing)
    assert sum(int(r[3].rstrip("%")) for r in finding.evidence.rows) == 100
    assert sum(money(r[2]) for r in finding.evidence.rows) == Decimal("50.00")


def test_within_a_tier_models_are_ordered_by_spend(pricing):
    """Both frontier. Same tokens, different list prices:

        claude-fable-5-1  1,000,000 uncached @ $10 = $10.00   first
        claude-opus-5     1,000,000 uncached @  $5 =  $5.00   second
    """
    finding = only(
        run(frame_of((seg("claude-opus-5"), uncached(1_000_000)), (seg("claude-fable-5-1"), uncached(1_000_000))), pricing)
    )
    assert [r[0] for r in finding.evidence.rows] == ["claude-fable-5-1", "claude-opus-5"]
    assert rows_by_model(finding)["claude-fable-5-1"][4] == "$10.00 / $50.00"


def test_spend_is_what_was_paid_while_list_price_is_the_rate_card(pricing):
    """Batch halves the bill, not the list price.

        claude-opus-5 batch, 2,000,000 uncached @ $2.50 = $5.00
    """
    finding = only(run(frame_of((seg("claude-opus-5", service_tier="batch"), uncached(2_000_000))), pricing))
    row = rows_by_model(finding)["claude-opus-5"]
    assert row[2] == "$5.00"
    assert row[4] == "$5.00 / $25.00"


def test_all_frontier_is_reported_as_such(pricing):
    finding = only(run(frame_of((seg("claude-opus-5"), uncached(2_000_000))), pricing))
    assert "100% frontier ($10.00)" in finding.summary


# --------------------------------------------------------------------------
# Tiers come from the pricing file
# --------------------------------------------------------------------------


def test_retiering_a_model_in_the_file_changes_the_result(pricing_data):
    """Call Sonnet 5 frontier and frontier spend becomes $30 + $10 = $40, 80%.
    The check holds no opinion about which model is which."""
    pricing_data["models"]["claude-sonnet-5"]["tier"] = "frontier"
    finding = golden(PricingTable(pricing_data))
    assert finding.measured_usd == Decimal("40.00")
    assert "80% frontier ($40.00)" in finding.summary
    assert rows_by_model(finding)["claude-sonnet-5"][1] == "frontier"


def test_a_model_with_no_tier_is_unclassified_not_dropped(pricing_data):
    """Dropping it would make the shares add up to less than the spend."""
    del pricing_data["models"]["claude-haiku-4-5"]["tier"]
    finding = golden(PricingTable(pricing_data))
    assert rows_by_model(finding)["claude-haiku-4-5"][1] == "unclassified"
    assert "20% unclassified ($10.00)" in finding.summary
    assert sum(int(r[3].rstrip("%")) for r in finding.evidence.rows) == 100
    assert any("$10.00 is on models the pricing file gives no tier" in c for c in finding.caveats)


def test_a_user_defined_tier_sorts_after_the_known_ones(pricing_data):
    pricing_data["models"]["claude-haiku-4-5"]["tier"] = "legacy"
    finding = golden(PricingTable(pricing_data))
    assert [r[1] for r in finding.evidence.rows] == ["frontier", "mid", "legacy"]


def test_no_unclassified_caveat_when_every_model_has_a_tier(pricing):
    assert not any("no tier" in c for c in golden(pricing).caveats)


# --------------------------------------------------------------------------
# One model, one row
# --------------------------------------------------------------------------


def test_display_name_and_id_are_one_row(pricing):
    """A single display-name spelling is never rewritten by alias merging,
    so C8 must key on the canonical id itself.

        "Claude Opus 5"  1M @ $5 = $5.00
        "claude-opus-5"  1M @ $5 = $5.00   ->  one row, $10.00
    """
    finding = only(
        run(frame_of((seg("Claude Opus 5"), uncached(1_000_000)), (seg("claude-opus-5"), uncached(1_000_000))), pricing)
    )
    assert finding.evidence.rows == (
        ("claude-opus-5", "frontier", "$10.00", "100%", "$5.00 / $25.00"),
    )


def test_a_dated_snapshot_is_shown_under_its_canonical_id(pricing):
    finding = only(
        run(frame_of((seg("claude-opus-5"), uncached(1_000_000)), (seg("claude-haiku-4-5-20251001"), uncached(1_000_000))), pricing)
    )
    assert "claude-haiku-4-5" in rows_by_model(finding)


# --------------------------------------------------------------------------
# Unpriced traffic
# --------------------------------------------------------------------------


def test_unknown_models_are_excluded_not_zeroed(pricing):
    finding = only(
        run(frame_of((seg("claude-opus-5"), uncached(1_000_000)), (seg("claude-imaginary-9"), uncached(50_000_000))), pricing)
    )
    assert list(rows_by_model(finding)) == ["claude-opus-5"]
    assert "100% frontier" in finding.summary


def test_unpriced_tiers_are_excluded(pricing):
    """Priority-tier Opus has no published rate; counting it would inflate
    frontier spend with a number the tool had to invent."""
    finding = only(
        run(
            frame_of(
                (seg("claude-opus-5"), uncached(1_000_000)),
                (seg("claude-opus-5", service_tier="priority"), uncached(50_000_000)),
                (seg("claude-sonnet-5"), uncached(2_500_000)),
            ),
            pricing,
        )
    )
    # $5.00 frontier of $10.00 priced
    assert finding.measured_usd == Decimal("5.00")
    assert "50% frontier ($5.00), 50% mid ($5.00)" in finding.summary


# --------------------------------------------------------------------------
# Staying quiet
# --------------------------------------------------------------------------


def test_no_frontier_spend_produces_nothing(pricing):
    frame = frame_of((seg("claude-sonnet-5"), uncached(5_000_000)), (seg("claude-haiku-4-5"), uncached(5_000_000)))
    assert run(frame, pricing) == []


def test_frontier_below_min_impact_produces_nothing(pricing):
    """100,000 uncached on Opus 5 = $0.50."""
    frame = frame_of((seg("claude-opus-5"), uncached(100_000)), (seg("claude-sonnet-5"), uncached(50_000_000)))
    assert run(frame, pricing) == []
    assert len(run(frame, pricing, min_impact_usd=Decimal("0.10"))) == 1


def test_empty_or_unpriced_data_produces_nothing(pricing):
    assert run(frame_of(), pricing) == []
    assert run(frame_of((seg("claude-imaginary-9"), uncached(1_000_000))), pricing) == []


# --------------------------------------------------------------------------
# No routing claim
# --------------------------------------------------------------------------


def test_claims_no_savings(pricing):
    finding = golden(pricing)
    assert finding.confidence is Confidence.OBSERVATION
    assert finding.recoverable is None
    assert "makes no routing claim" in finding.summary


def test_says_sizing_needs_per_request_analysis(pricing):
    assert any("per-request analysis" in c for c in golden(pricing).caveats)


def test_cites_the_routing_research_with_its_limits(pricing):
    """The brief asks for the research to be mentioned. It is quoted at the
    size its authors claimed, with the reasons it may not transfer."""
    citation = next(c for c in golden(pricing).caveats if "RouteLLM" in c)
    assert "arXiv:2406.18665" in citation
    assert "by over 2 times in certain cases" in citation
    assert "without per-request evaluation" in citation


def test_says_the_tiers_are_this_tools_classification(pricing):
    assert "not Anthropic's" in golden(pricing).evidence.note


def test_no_dollar_figure_but_the_measured_ones(pricing):
    """An overclaim on this check would discredit the whole report. No sentence may pair a dollar amount with saving or routing."""
    finding = golden(pricing)
    for text in (finding.summary, finding.remedy, *finding.caveats):
        lowered = text.lower()
        assert "could save" not in lowered
        assert "would save" not in lowered
        assert "savings of $" not in lowered


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


def test_runs_from_the_registry(pricing):
    assert "C8" in {f.check_id for f in run_checks(frame_of(*MIX), pricing).findings}


@pytest.mark.parametrize("renderer", ["terminal", "markdown"])
def test_renders_without_renderer_changes(pricing, renderer):
    from claude_spend_audit.render import markdown, terminal

    finding = golden(pricing)
    if renderer == "terminal":
        text = "\n".join(terminal.render_findings([finding]))
    else:
        text = "\n".join(markdown._render_finding(finding))
    assert "C8" in text
    assert "$30.00" in text
    assert "not claimed" in text
