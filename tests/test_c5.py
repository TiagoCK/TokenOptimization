"""C5 -- output token share, with every figure hand-calculated.

Rates used, all from pricing/2026-09.json:

    claude-opus-5     input $5   read $0.50   output $25   (output 5x input)
    claude-sonnet-5   input $2   read $0.20   output $10   (output 5x input)

The trigger is output's share of *tokens*. Spend share is reported beside it
but never gates the finding, because caching moves spend share without
changing what a workload writes relative to what it reads.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from claude_spend_audit.checks import CheckConfig, run_checks
from claude_spend_audit.checks.c5_output_share import CHECK as C5
from claude_spend_audit.findings import Confidence
from claude_spend_audit.frame import Frame, Segment, TokenBuckets, UsageRow
from claude_spend_audit.pricing import PricingTable

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 2, tzinfo=timezone.utc)

GROUPED = frozenset({"model", "workspace_id", "service_tier", "inference_geo"})


def seg(**kw) -> Segment:
    base = dict(
        model="claude-opus-5",
        workspace_id="wrkspc_writing",
        service_tier="standard",
        inference_geo="global",
        speed="standard",
    )
    return Segment(**(base | kw))


def frame_of(*rows: tuple[Segment, TokenBuckets], grouped=GROUPED) -> Frame:
    return Frame(
        usage=tuple(UsageRow(start=T0, end=T1, segment=s, tokens=t) for s, t in rows),
        grouped_by=frozenset(grouped),
        bucket_width="1d",
    )


def run(frame, pricing, **config):
    return list(C5.run(frame, pricing, CheckConfig(**config)))


def only(findings):
    assert len(findings) == 1, f"expected one finding, got {len(findings)}"
    return findings[0]


def labels(finding) -> list[str]:
    return [row[0] for row in finding.evidence.rows]


#: The golden case. claude-opus-5 / wrkspc_writing:
#:
#:     uncached  1,000,000 @  $5.00/MTok =  $5.00
#:     output    1,000,000 @ $25.00/MTok = $25.00
#:                                         ------
#:                                         $30.00
#:
#:     output share of tokens  1M / 2M      = 50%
#:     output share of spend   $25 / $30    = 83.3% -> 83%
#:     measured (output spend)              = $25.00
EVEN = TokenBuckets(uncached_input=1_000_000, output=1_000_000)


def golden(pricing):
    return only(run(frame_of((seg(), EVEN)), pricing))


# --------------------------------------------------------------------------
# The golden figures
# --------------------------------------------------------------------------


def test_output_spend_is_measured(pricing):
    finding = golden(pricing)
    assert finding.check_id == "C5"
    assert finding.measured_usd == Decimal("25.00")
    assert finding.scope == "1 segment(s)"


def test_token_share_and_spend_share_are_both_stated(pricing):
    finding = golden(pricing)
    assert "output is 50% of tokens but 83% of spend ($25.00)" in finding.summary
    assert finding.evidence.rows == (
        ("claude-opus-5 / wrkspc_writing", "1.0M", "50%", "$25.00", "83%"),
    )


def test_the_price_multiple_is_stated(pricing):
    assert "Output costs 5x as much as uncached input on this model" in golden(pricing).summary


def test_the_threshold_is_stated_in_the_finding(pricing):
    assert "at least 25% of the segment's tokens" in golden(pricing).evidence.note


# --------------------------------------------------------------------------
# The multiple is read from the rate card, never written in the code
# --------------------------------------------------------------------------


def test_the_multiple_follows_the_rate_card(pricing_data):
    """Edit Opus 5's output rate to $20 and the finding says 4x.

        uncached 1,000,000 @ $5  =  $5.00
        output   1,000,000 @ $20 = $20.00   ->  80% of $25.00 spend
    """
    pricing_data["models"]["claude-opus-5"]["rates"]["output"]["usd_per_mtok"] = "20"
    finding = golden(PricingTable(pricing_data))
    assert "Output costs 4x as much" in finding.summary
    assert "5x" not in finding.summary
    assert finding.measured_usd == Decimal("20.00")
    assert "80% of spend" in finding.summary


def test_differing_multiples_are_given_as_a_range(pricing_data):
    """Sonnet 5 output edited to $8 (4x its $2 input); Opus 5 stays 5x."""
    pricing_data["models"]["claude-sonnet-5"]["rates"]["output"]["usd_per_mtok"] = "8"
    finding = only(
        run(
            frame_of(
                (seg(), EVEN),
                (seg(model="claude-sonnet-5", workspace_id="wrkspc_b"), EVEN),
            ),
            PricingTable(pricing_data),
        )
    )
    assert "between 4x and 5x as much as uncached input on these models" in finding.summary


def test_the_same_multiple_on_several_models_is_one_figure(pricing):
    finding = only(
        run(
            frame_of(
                (seg(), EVEN),
                (seg(model="claude-sonnet-5", workspace_id="wrkspc_b"), EVEN),
            ),
            pricing,
        )
    )
    assert "Output costs 5x as much as uncached input on these models" in finding.summary


# --------------------------------------------------------------------------
# Caching must not decide the outcome
# --------------------------------------------------------------------------


def test_caching_does_not_change_the_decision(pricing):
    """The design property. Two workloads with identical token counts, one
    cached and one not:

        uncached:  4,000,000 uncached ($20.00) + 1,000,000 output ($25.00)
                   20% of tokens, $25 / $45 = 56% of spend
        cached:    4,000,000 reads    ($2.00)  + 1,000,000 output ($25.00)
                   20% of tokens, $25 / $27 = 93% of spend

    Judged on spend, the cached one would be flagged for having cached. On
    tokens they are the same workload, and they get the same answer.
    """
    frame = frame_of(
        (seg(workspace_id="wrkspc_uncached"), TokenBuckets(uncached_input=4_000_000, output=1_000_000)),
        (seg(workspace_id="wrkspc_cached"), TokenBuckets(cache_read=4_000_000, output=1_000_000)),
    )
    assert run(frame, pricing) == []

    finding = only(run(frame, pricing, output_min_token_share=Decimal("0.20")))
    rows = {row[0]: row for row in finding.evidence.rows}
    assert rows["claude-opus-5 / wrkspc_uncached"][2:] == ("20%", "$25.00", "56%")
    assert rows["claude-opus-5 / wrkspc_cached"][2:] == ("20%", "$25.00", "93%")


def test_a_high_spend_share_alone_does_not_trigger(pricing):
    """93% of spend on output, but only 20% of tokens: not shown."""
    frame = frame_of((seg(), TokenBuckets(cache_read=4_000_000, output=1_000_000)))
    assert run(frame, pricing) == []


# --------------------------------------------------------------------------
# Several segments
# --------------------------------------------------------------------------


def test_segments_are_ordered_by_output_spend(pricing):
    """opus-5 / wrkspc_a     1M output @ $25 = $25.00
    sonnet-5 / wrkspc_b   1M output @ $10 = $10.00

    Combined: 2M of 4M tokens = 50%; $35 of $42 spend = 83%.
    """
    finding = only(
        run(
            frame_of(
                (seg(model="claude-sonnet-5", workspace_id="wrkspc_b"), EVEN),
                (seg(workspace_id="wrkspc_a"), EVEN),
            ),
            pricing,
        )
    )
    assert labels(finding) == ["claude-opus-5 / wrkspc_a", "claude-sonnet-5 / wrkspc_b"]
    assert finding.measured_usd == Decimal("35.00")
    assert finding.scope == "2 segment(s)"
    assert "50% of tokens but 83% of spend ($35.00)" in finding.summary


def test_segments_differing_only_by_routing_roll_up(pricing):
    """Same model and workspace, one global and one us (1.1x):

        global  output 1M @ $25.00 = $25.00, uncached 1M @ $5.00 = $5.00
        us      output 1M @ $27.50 = $27.50, uncached 1M @ $5.50 = $5.50
                                     ------                        -----
                                     $52.50                        $10.50

    One row: 2M output of 4M = 50%; $52.50 / $63.00 = 83%.
    """
    finding = only(
        run(frame_of((seg(), EVEN), (seg(inference_geo="us"), EVEN)), pricing)
    )
    assert finding.evidence.rows == (
        ("claude-opus-5 / wrkspc_writing", "2.0M", "50%", "$52.50", "83%"),
    )


def test_only_segments_over_the_threshold_are_shown(pricing):
    finding = only(
        run(
            frame_of(
                (seg(workspace_id="wrkspc_heavy"), EVEN),
                (seg(workspace_id="wrkspc_light"), TokenBuckets(uncached_input=9_000_000, output=1_000_000)),
            ),
            pricing,
        )
    )
    assert labels(finding) == ["claude-opus-5 / wrkspc_heavy"]
    assert finding.measured_usd == Decimal("25.00")


# --------------------------------------------------------------------------
# Unpriced traffic and ungrouped exports
# --------------------------------------------------------------------------


def test_unpriced_segments_are_ignored(pricing):
    """Priority-tier output has no price, so it can neither trigger the check
    nor dilute a priced segment's share."""
    frame = frame_of(
        (seg(service_tier="priority"), TokenBuckets(output=50_000_000)),
        (seg(workspace_id="wrkspc_priced"), EVEN),
    )
    finding = only(run(frame, pricing))
    assert labels(finding) == ["claude-opus-5 / wrkspc_priced"]
    assert finding.measured_usd == Decimal("25.00")


def test_entirely_unpriced_data_produces_nothing(pricing):
    assert run(frame_of((seg(model="claude-imaginary-9"), EVEN)), pricing) == []


def test_an_export_without_workspaces_reports_one_blended_segment(pricing):
    """No workspace column: the row covers every workspace, and says so."""
    frame = frame_of(
        (seg(workspace_id=None), EVEN), grouped={"model", "service_tier"}
    )
    finding = only(run(frame, pricing))
    assert labels(finding) == ["claude-opus-5 / all workspaces"]


def test_runs_without_any_dimension_requirement():
    assert C5.needs_dimensions == frozenset()


# --------------------------------------------------------------------------
# Staying quiet
# --------------------------------------------------------------------------


def test_below_the_threshold_produces_nothing(pricing):
    """4M uncached + 1M output: 20%, under the 25% default."""
    frame = frame_of((seg(), TokenBuckets(uncached_input=4_000_000, output=1_000_000)))
    assert run(frame, pricing) == []
    assert len(run(frame, pricing, output_min_token_share=Decimal("0.20"))) == 1


def test_exactly_at_the_threshold_is_shown(pricing):
    """3M uncached + 1M output = 25% exactly. The threshold is inclusive."""
    frame = frame_of((seg(), TokenBuckets(uncached_input=3_000_000, output=1_000_000)))
    assert len(run(frame, pricing)) == 1


def test_below_min_impact_produces_nothing(pricing):
    """10,000 output @ $25/MTok = $0.25 of output spend."""
    frame = frame_of((seg(), TokenBuckets(uncached_input=10_000, output=10_000)))
    assert run(frame, pricing) == []
    assert len(run(frame, pricing, min_impact_usd=Decimal("0.10"))) == 1


def test_no_output_at_all_produces_nothing(pricing):
    assert run(frame_of((seg(), TokenBuckets(uncached_input=10_000_000))), pricing) == []


def test_empty_data_produces_nothing(pricing):
    assert run(frame_of(), pricing) == []


# --------------------------------------------------------------------------
# An observation, with the caveats the data forces
# --------------------------------------------------------------------------


def test_claims_no_savings(pricing):
    finding = golden(pricing)
    assert finding.confidence is Confidence.OBSERVATION
    assert finding.recoverable is None
    assert "claims no saving" in finding.summary


def test_says_output_includes_thinking(pricing):
    """Thinking is billed as output and the usage report cannot separate it.
    Without this, a reasoning-heavy workload reads as a verbose one."""
    assert any("include thinking" in c for c in golden(pricing).caveats)


def test_says_there_are_no_request_counts(pricing):
    assert any("no request counts" in c for c in golden(pricing).caveats)


def test_says_some_workloads_are_generative(pricing):
    assert any("legitimately generative" in c for c in golden(pricing).caveats)


def test_remedy_names_the_levers(pricing):
    remedy = golden(pricing).remedy
    assert "max_tokens" in remedy
    assert "effort" in remedy
    assert "thinking_tokens" in remedy
    # Changing effort breaks the cache once; saying so stops a C5 fix
    # turning into a C1 finding.
    assert "invalidates the prompt cache once" in remedy


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


def test_runs_from_the_registry(pricing):
    result = run_checks(frame_of((seg(), EVEN)), pricing, CheckConfig())
    assert "C5" in {f.check_id for f in result.findings}


@pytest.mark.parametrize("renderer", ["terminal", "markdown"])
def test_renders_without_renderer_changes(pricing, renderer):
    from claude_spend_audit.render import markdown, terminal

    finding = golden(pricing)
    if renderer == "terminal":
        text = "\n".join(terminal.render_findings([finding]))
    else:
        text = "\n".join(markdown._render_finding(finding))
    assert "C5" in text
    assert "$25.00" in text
    assert "not claimed" in text
