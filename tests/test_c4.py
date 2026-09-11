"""C4 -- long-context composition, with every figure hand-calculated.

Rates used, all from pricing/2026-09.json:

    claude-opus-5      input $5    read $0.50   5m write $6.25   output $25
    claude-sonnet-5    input $2    read $0.20                    output $10
    claude-fable-5-1   input $10   read $0.25  (0.025x, not 0.1x)
    claude-sonnet-4-5  input $3    read $0.30

The check is an observation. Its value is the composition of long-context
spend and the effective rate per category -- never a savings figure.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from claude_spend_audit.checks import CheckConfig, run_checks
from claude_spend_audit.checks.c4_long_context import CHECK as C4
from claude_spend_audit.findings import Confidence
from claude_spend_audit.frame import Frame, Segment, TokenBuckets, UsageRow
from claude_spend_audit.gaps import GapCode
from claude_spend_audit.ingest import load_usage_csv

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 2, tzinfo=timezone.utc)

GROUPED = frozenset({"model", "workspace_id", "service_tier", "context_window"})


def seg(**kw) -> Segment:
    base = dict(
        model="claude-opus-5",
        workspace_id="wrkspc_research",
        service_tier="standard",
        inference_geo="global",
        speed="standard",
        context_window="200k-1M",
    )
    return Segment(**(base | kw))


def frame_of(*rows: tuple[Segment, TokenBuckets], grouped=GROUPED) -> Frame:
    return Frame(
        usage=tuple(UsageRow(start=T0, end=T1, segment=s, tokens=t) for s, t in rows),
        grouped_by=frozenset(grouped),
        bucket_width="1d",
    )


def run(frame, pricing, **config):
    return list(C4.run(frame, pricing, CheckConfig(**config)))


def only(findings):
    assert len(findings) == 1, f"expected one finding, got {len(findings)}"
    return findings[0]


def row(finding, label: str) -> tuple[str, ...]:
    matches = [r for r in finding.evidence.rows if r[0].startswith(label)]
    assert len(matches) == 1, f"no single evidence row for {label!r}"
    return matches[0]


def money(cell: str) -> Decimal:
    return Decimal(cell.lstrip("$").replace(",", ""))


#: The golden case.
#:
#:   200k-1M, claude-opus-5:
#:     uncached     2,000,000 @  $5.00/MTok = $10.00
#:     cache read   4,000,000 @  $0.50/MTok =  $2.00
#:     output         100,000 @ $25.00/MTok =  $2.50
#:                                            ------
#:     long-context spend                     $14.50
#:
#:   0-200k, claude-opus-5:
#:     uncached     1,000,000 @  $5.00/MTok =  $5.00
#:
#:   total priced spend $19.50; long-context share 14.50 / 19.50 = 74.4% -> 74%
#:   uncached share of long-context spend 10.00 / 14.50 = 69.0% -> 69%
LONG = TokenBuckets(uncached_input=2_000_000, cache_read=4_000_000, output=100_000)
SHORT = TokenBuckets(uncached_input=1_000_000)


def golden(pricing):
    return only(
        run(frame_of((seg(), LONG), (seg(context_window="0-200k"), SHORT)), pricing)
    )


# --------------------------------------------------------------------------
# Preconditions
# --------------------------------------------------------------------------


def test_ungrouped_context_window_blocks_the_check(pricing):
    """Unknown is not "short". Reading an absent dimension as 0-200k would
    report a confident 0% the data never gave."""
    frame = frame_of((seg(context_window=None), LONG), grouped={"model"})
    unmet = C4.unmet(frame)
    assert [g.code for g in unmet] == [GapCode.DIMENSION_NOT_GROUPED]
    assert "Long-context composition (C4)" in unmet[0].message

    result = run_checks(frame, pricing, CheckConfig())
    assert not [f for f in result.findings if f.check_id == "C4"]
    assert "Long-context composition (C4)" in [label for label, _ in result.skipped]


def test_a_csv_without_the_column_is_skipped_not_zeroed(pricing, usage_profile, tmp_path):
    path = tmp_path / "no_context.csv"
    path.write_text(
        "start,model,uncached_input_tokens,output_tokens\n"
        "2026-08-01T00:00:00Z,claude-opus-5,1000000,100000\n",
        encoding="utf-8",
    )
    frame, _ = load_usage_csv(path, usage_profile)
    assert C4.unmet(frame)


# --------------------------------------------------------------------------
# The golden figures
# --------------------------------------------------------------------------


def test_long_context_spend_and_share(pricing):
    finding = golden(pricing)
    assert finding.check_id == "C4"
    assert finding.measured_usd == Decimal("14.50")
    assert finding.scope == "74% of priced spend"
    assert "$14.50 (74% of priced spend)" in finding.summary


def test_the_summary_points_at_the_uncached_share(pricing):
    assert "69% of that was uncached input" in golden(pricing).summary


def test_composition_by_category(pricing):
    """$10.00 / $14.50 = 69%, $2.00 / $14.50 = 14%, $2.50 / $14.50 = 17%."""
    finding = golden(pricing)
    assert row(finding, "uncached input")[1:4] == ("2.0M", "$10.00", "69%")
    assert row(finding, "cache read")[1:4] == ("4.0M", "$2.00", "14%")
    assert row(finding, "output")[1:4] == ("100k", "$2.50", "17%")


def test_effective_rates_and_their_ratio(pricing):
    """The "value per 1M removed" table from the brief, measured.

        uncached  $10.00 / 2M = $5.00/MTok   1x
        read       $2.00 / 4M = $0.50/MTok   0.1x
    """
    finding = golden(pricing)
    assert row(finding, "uncached input")[4:] == ("$5.00", "1x")
    assert row(finding, "cache read")[4:] == ("$0.50", "0.1x")


def test_the_leverage_sentence_uses_the_measured_ratio(pricing):
    assert "worth 0.1x as much to remove" in golden(pricing).summary


def test_output_is_labelled_as_not_context(pricing):
    """Output is generated, not sent. It is in the table so the spend column
    reconciles, and labelled so nobody reads it as prunable context."""
    assert row(golden(pricing), "output")[0] == "output (not context)"


def test_the_spend_column_adds_up_to_the_measured_figure(pricing):
    finding = golden(pricing)
    assert sum(money(r[2]) for r in finding.evidence.rows) == finding.measured_usd


def test_categories_appear_in_a_stable_order(pricing):
    labels = [r[0] for r in golden(pricing).evidence.rows]
    assert labels == ["uncached input", "cache read", "output (not context)"]


def test_every_category_present_is_shown(pricing):
    """Both cache-write TTLs, when present, get their own row.

        5m write 800,000 @ $6.25/MTok = $5.00  ->  $6.25/MTok, 1.25x
        1h write 500,000 @ $10.00/MTok = $5.00 ->  $10.00/MTok, 2x
    """
    tokens = TokenBuckets(
        uncached_input=1_000_000, cache_write_5m=800_000, cache_write_1h=500_000
    )
    finding = only(run(frame_of((seg(), tokens)), pricing))
    assert row(finding, "cache write (5m)")[4:] == ("$6.25", "1.25x")
    assert row(finding, "cache write (1h)")[4:] == ("$10.00", "2x")


# --------------------------------------------------------------------------
# Effective rates come from the data, not from a multiplier in the code
# --------------------------------------------------------------------------


def test_effective_rate_blends_models(pricing):
    """Two models in the window. The rate is what was actually paid.

        claude-opus-5    1,000,000 uncached @ $5.00 = $5.00
        claude-sonnet-5  1,000,000 uncached @ $2.00 = $2.00
                                                      -----
                         2,000,000                    $7.00  ->  $3.50/MTok
    """
    tokens = TokenBuckets(uncached_input=1_000_000)
    finding = only(
        run(frame_of((seg(), tokens), (seg(model="claude-sonnet-5"), tokens)), pricing)
    )
    assert row(finding, "uncached input")[4] == "$3.50"


def test_fable_cache_reads_show_their_own_ratio(pricing):
    """Fable 5.1 reads are 0.025x base, not 0.1x. A hardcoded 0.1 would get
    this wrong; the measured rate cannot.

        uncached  1,000,000 @ $10.00 = $10.00  ->  $10.00/MTok
        read      4,000,000 @  $0.25 =  $1.00  ->   $0.25/MTok  ->  0.025x
    """
    tokens = TokenBuckets(uncached_input=1_000_000, cache_read=4_000_000)
    finding = only(run(frame_of((seg(model="claude-fable-5-1"), tokens)), pricing))
    assert row(finding, "cache read")[4:] == ("$0.25", "0.025x")
    assert "0.025x as much" in finding.summary


def test_us_routing_is_reflected_in_the_rate(pricing):
    """1,000,000 uncached on claude-opus-5 at 1.1x (us) = $5.50/MTok."""
    finding = only(
        run(frame_of((seg(inference_geo="us"), TokenBuckets(uncached_input=1_000_000))), pricing)
    )
    assert row(finding, "uncached input")[4] == "$5.50"


def test_no_uncached_input_means_no_ratio_rather_than_a_division_error(pricing):
    """All cache reads, no uncached input: there is nothing to be relative to.

        cache read 40,000,000 @ $0.50 = $20.00
    """
    finding = only(
        run(frame_of((seg(), TokenBuckets(cache_read=40_000_000))), pricing)
    )
    assert row(finding, "cache read")[5] == "-"
    assert "worth" not in finding.summary
    assert "0% of that was uncached input" in finding.summary


# --------------------------------------------------------------------------
# Unpriced traffic
# --------------------------------------------------------------------------


def test_unpriced_rows_are_excluded_from_both_sides(pricing):
    """Priority-tier rows have no price. Counting their tokens against priced
    spend would drag every effective rate down.

        opus-5 standard, long:   1,000,000 uncached = $5.00  (priced)
        opus-5 priority, long:   9,000,000 uncached = unpriced, excluded
        opus-5 standard, short:  1,000,000 uncached = $5.00

        long share = 5 / 10 = 50%, rate $5.00/MTok on 1.0M tokens
    """
    tokens = TokenBuckets(uncached_input=1_000_000)
    finding = only(
        run(
            frame_of(
                (seg(), tokens),
                (seg(service_tier="priority"), TokenBuckets(uncached_input=9_000_000)),
                (seg(context_window="0-200k"), tokens),
            ),
            pricing,
        )
    )
    assert finding.measured_usd == Decimal("5.00")
    assert finding.scope == "50% of priced spend"
    assert row(finding, "uncached input")[1] == "1.0M"
    assert row(finding, "uncached input")[4] == "$5.00"


def test_entirely_unpriced_data_produces_nothing(pricing):
    frame = frame_of((seg(model="claude-imaginary-9"), LONG))
    assert run(frame, pricing) == []


# --------------------------------------------------------------------------
# Staying quiet
# --------------------------------------------------------------------------


def test_all_short_context_produces_nothing(pricing):
    assert run(frame_of((seg(context_window="0-200k"), LONG)), pricing) == []


def test_below_the_share_threshold_produces_nothing(pricing):
    """Long $5.00 against short $50.00: 5 / 55 = 9%, under the 10% default."""
    frame = frame_of(
        (seg(), TokenBuckets(uncached_input=1_000_000)),
        (seg(context_window="0-200k"), TokenBuckets(uncached_input=10_000_000)),
    )
    assert run(frame, pricing) == []
    assert len(run(frame, pricing, long_context_min_share=Decimal("0.05"))) == 1


def test_below_min_impact_produces_nothing(pricing):
    """100,000 uncached = $0.50 long-context spend; 100% share, but trivial."""
    frame = frame_of((seg(), TokenBuckets(uncached_input=100_000)))
    assert run(frame, pricing) == []
    assert len(run(frame, pricing, min_impact_usd=Decimal("0.10"))) == 1


def test_empty_data_produces_nothing(pricing):
    assert run(frame_of(), pricing) == []


# --------------------------------------------------------------------------
# It is an observation, and says the things the brief requires
# --------------------------------------------------------------------------


def test_claims_no_savings(pricing):
    finding = golden(pricing)
    assert finding.confidence is Confidence.OBSERVATION
    assert finding.recoverable is None
    assert "estimates no saving" in finding.summary


def test_states_that_there_is_no_premium(pricing):
    """Any finding implying a premium is recoverable would be claiming
    something false, and a technical buyer would know it."""
    caveats = " ".join(golden(pricing).caveats)
    assert "no long-context premium" in caveats
    assert "same per-token rate" in caveats


def test_states_the_cache_interaction_correctly(pricing):
    """The intuitive version -- pruning keeps costing re-writes -- is wrong,
    and the finding must not repeat it."""
    caveats = " ".join(golden(pricing).caveats)
    assert "invalidates it once" in caveats
    assert "one-time transition cost" in caveats
    assert "recurring" not in caveats


def test_carries_the_tokenizer_caveat(pricing):
    assert any("30% more tokens" in c for c in golden(pricing).caveats)


def test_says_long_context_is_often_load_bearing(pricing):
    assert any("load-bearing" in c for c in golden(pricing).caveats)


def test_remedy_starts_with_the_uncached_pile(pricing):
    assert golden(pricing).remedy.startswith("Start with the uncached input")


# --------------------------------------------------------------------------
# Models the pricing page does not confirm
# --------------------------------------------------------------------------


def test_pre_4_6_long_context_is_named_in_a_caveat(pricing):
    """claude-sonnet-4-5, 1,000,000 uncached + 1,000,000 output in 200k-1M.
    Priced at standard ($3 + $15 = $18.00) and labelled as unconfirmed."""
    tokens = TokenBuckets(uncached_input=1_000_000, output=1_000_000)
    finding = only(run(frame_of((seg(model="claude-sonnet-4-5"), tokens)), pricing))
    assert finding.measured_usd == Decimal("18.00")
    unconfirmed = [c for c in finding.caveats if "older than Claude 4.6" in c]
    assert len(unconfirmed) == 1
    assert "claude-sonnet-4-5 (2,000,000 tokens)" in unconfirmed[0]
    assert "may be understated" in unconfirmed[0]


def test_4_6_and_later_carry_no_such_caveat(pricing):
    assert not any("older than Claude 4.6" in c for c in golden(pricing).caveats)


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------


def test_runs_from_the_registry(pricing):
    frame = frame_of((seg(), LONG), (seg(context_window="0-200k"), SHORT))
    result = run_checks(frame, pricing, CheckConfig())
    assert [f.check_id for f in result.findings if f.check_id == "C4"] == ["C4"]


@pytest.mark.parametrize("renderer", ["terminal", "markdown"])
def test_renders_without_renderer_changes(pricing, renderer):
    """§7: adding a check never requires touching a reporter."""
    from claude_spend_audit.render import markdown, terminal

    finding = golden(pricing)
    if renderer == "terminal":
        text = "\n".join(terminal.render_findings([finding]))
    else:
        text = "\n".join(markdown._render_finding(finding))
    assert "C4" in text
    assert "$14.50" in text
    assert "not claimed" in text
