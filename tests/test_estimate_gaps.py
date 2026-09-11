"""Gaps about the estimate as a whole, raised by PricingTable.estimate_frame.

Two ways a total can be silently low without any single rate being wrong:

* a rate applied that the pricing page never confirmed -- long-context traffic
  on a model older than Claude 4.6
* a price-changing dimension the export never broke out, so every row prices
  at the baseline

Both are the "never a silent wrong total" rule applied to the estimate itself.
Neither sets ``invalidates_total``: that flag drives the exit code and means
the total is *known* to understate, which is not what either of these says.
"""

from datetime import datetime, timezone
from decimal import Decimal

from claude_spend_audit.cli import _exit_code
from claude_spend_audit.frame import Frame, Segment, TokenBuckets, UsageRow
from claude_spend_audit.gaps import GapCode
from claude_spend_audit.ingest import load_usage_csv

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 2, tzinfo=timezone.utc)

EVERY_PRICE_DIMENSION = frozenset(
    {"model", "service_tier", "inference_geo", "speed", "context_window"}
)


def frame_of(*rows, grouped=EVERY_PRICE_DIMENSION) -> Frame:
    return Frame(
        usage=tuple(
            UsageRow(start=T0, end=T1, segment=s, tokens=t) for s, t in rows
        ),
        grouped_by=frozenset(grouped),
        bucket_width="1d",
    )


def seg(**kw) -> Segment:
    base = dict(
        model="claude-opus-5",
        service_tier="standard",
        inference_geo="global",
        speed="standard",
        context_window="0-200k",
    )
    return Segment(**(base | kw))


def codes(estimate) -> set:
    return {g.code for g in estimate.gaps}


def gap(estimate, code):
    found = [g for g in estimate.gaps if g.code is code]
    assert len(found) == 1, f"expected one {code}, got {len(found)}"
    return found[0]


MTOK = TokenBuckets(uncached_input=1_000_000)


# --------------------------------------------------------------------------
# Long-context rate unconfirmed
# --------------------------------------------------------------------------


def test_pre_4_6_long_context_is_flagged(pricing):
    estimate = pricing.estimate_frame(
        frame_of((seg(model="claude-sonnet-4-5", context_window="200k-1M"), MTOK))
    )
    found = gap(estimate, GapCode.LONG_CONTEXT_RATE_UNCONFIRMED)
    assert found.scope == "claude-sonnet-4-5"
    assert "1,000,000 tokens" in found.message


def test_the_flagged_tokens_are_still_priced(pricing):
    """Flagged, not excluded. The standard rate is the only figure the file
    supports, so it is used -- and labelled.

        1,000,000 uncached on claude-sonnet-4-5 @ $3.00/MTok = $3.00
    """
    estimate = pricing.estimate_frame(
        frame_of((seg(model="claude-sonnet-4-5", context_window="200k-1M"), MTOK))
    )
    assert estimate.usd == Decimal("3.00")
    assert estimate.is_complete


def test_4_6_and_later_long_context_is_not_flagged(pricing):
    estimate = pricing.estimate_frame(
        frame_of((seg(model="claude-opus-5", context_window="200k-1M"), MTOK))
    )
    assert GapCode.LONG_CONTEXT_RATE_UNCONFIRMED not in codes(estimate)


def test_pre_4_6_short_context_is_not_flagged(pricing):
    estimate = pricing.estimate_frame(
        frame_of((seg(model="claude-sonnet-4-5", context_window="0-200k"), MTOK))
    )
    assert GapCode.LONG_CONTEXT_RATE_UNCONFIRMED not in codes(estimate)


def test_tokens_are_summed_per_model_across_rows(pricing):
    """Gaps dedupe on (code, scope), keeping the first. A per-row count would
    report the first row's tokens and silently drop the rest."""
    long = seg(model="claude-sonnet-4-5", context_window="200k-1M")
    estimate = pricing.estimate_frame(frame_of((long, MTOK), (long, MTOK), (long, MTOK)))
    assert "3,000,000 tokens" in gap(estimate, GapCode.LONG_CONTEXT_RATE_UNCONFIRMED).message


def test_one_gap_per_unconfirmed_model(pricing):
    estimate = pricing.estimate_frame(
        frame_of(
            (seg(model="claude-sonnet-4-5", context_window="200k-1M"), MTOK),
            (seg(model="claude-sonnet-4", context_window="200k-1M"), MTOK),
        )
    )
    scopes = {
        g.scope for g in estimate.gaps if g.code is GapCode.LONG_CONTEXT_RATE_UNCONFIRMED
    }
    assert scopes == {"claude-sonnet-4-5", "claude-sonnet-4"}


def test_spellings_of_one_model_share_a_gap(pricing):
    """A dated snapshot ID and the canonical ID are the same model."""
    estimate = pricing.estimate_frame(
        frame_of(
            (seg(model="claude-sonnet-4-5", context_window="200k-1M"), MTOK),
            (seg(model="claude-sonnet-4-5-20250929", context_window="200k-1M"), MTOK),
        )
    )
    found = gap(estimate, GapCode.LONG_CONTEXT_RATE_UNCONFIRMED)
    assert "2,000,000 tokens" in found.message


def test_unpriced_long_context_is_not_double_reported(pricing):
    """Priority-tier traffic is already unpriced with its own gap. Calling its
    long-context rate "unconfirmed" would imply it had been priced."""
    estimate = pricing.estimate_frame(
        frame_of(
            (seg(model="claude-sonnet-4-5", context_window="200k-1M", service_tier="priority"), MTOK)
        )
    )
    assert GapCode.LONG_CONTEXT_RATE_UNCONFIRMED not in codes(estimate)
    assert GapCode.PRIORITY_TIER_PRESENT in codes(estimate)


def test_the_confirmation_list_is_read_from_the_file(pricing_data):
    """Add Sonnet 4.5 to the confirmed list and the gap disappears. The code
    holds no opinion about which models are confirmed."""
    from claude_spend_audit.pricing import PricingTable

    pricing_data["long_context"]["standard_rate_models"].append("claude-sonnet-4-5")
    edited = PricingTable(pricing_data)
    estimate = edited.estimate_frame(
        frame_of((seg(model="claude-sonnet-4-5", context_window="200k-1M"), MTOK))
    )
    assert GapCode.LONG_CONTEXT_RATE_UNCONFIRMED not in codes(estimate)


# --------------------------------------------------------------------------
# Price-changing dimensions not grouped
# --------------------------------------------------------------------------


def test_fully_grouped_data_raises_nothing(pricing):
    estimate = pricing.estimate_frame(frame_of((seg(), MTOK)))
    assert GapCode.PRICING_DIMENSION_NOT_GROUPED not in codes(estimate)


def test_missing_geo_and_speed_are_named(pricing):
    estimate = pricing.estimate_frame(
        frame_of((seg(), MTOK), grouped={"model", "service_tier"})
    )
    found = gap(estimate, GapCode.PRICING_DIMENSION_NOT_GROUPED)
    assert found.scope == "inference_geo, speed"
    assert "1.1x" in found.message
    assert "fast mode" in found.message


def test_missing_service_tier_says_which_way_it_errs(pricing):
    """Batch overstated, priority priced when it should not be. The direction
    matters to anyone deciding whether the total is a floor or a ceiling."""
    estimate = pricing.estimate_frame(
        frame_of((seg(), MTOK), grouped={"model", "inference_geo", "speed"})
    )
    found = gap(estimate, GapCode.PRICING_DIMENSION_NOT_GROUPED)
    assert found.scope == "service_tier"
    assert "overstated" in found.message


def test_a_cost_only_frame_raises_nothing(pricing):
    """No usage rows means nothing was priced from tokens at all."""
    assert GapCode.PRICING_DIMENSION_NOT_GROUPED not in codes(
        pricing.estimate_frame(Frame(grouped_by=frozenset()))
    )


def test_a_csv_without_the_columns_raises_it(pricing, usage_profile, tmp_path):
    path = tmp_path / "plain.csv"
    path.write_text(
        "start,model,service_tier,uncached_input_tokens,output_tokens\n"
        "2026-08-01T00:00:00Z,claude-opus-5,standard,1000000,100000\n",
        encoding="utf-8",
    )
    frame, _ = load_usage_csv(path, usage_profile)
    found = gap(pricing.estimate_frame(frame), GapCode.PRICING_DIMENSION_NOT_GROUPED)
    assert found.scope == "inference_geo, speed"


def test_the_shipped_fixture_is_fully_grouped(pricing, usage_profile, usage_csv):
    frame, _ = load_usage_csv(usage_csv, usage_profile)
    assert GapCode.PRICING_DIMENSION_NOT_GROUPED not in codes(pricing.estimate_frame(frame))


# --------------------------------------------------------------------------
# Neither changes the exit code
# --------------------------------------------------------------------------


def test_neither_gap_changes_the_exit_code(pricing):
    """Exit 1 means the total is known to understate. These say it might."""
    frame = frame_of(
        (seg(model="claude-sonnet-4-5", context_window="200k-1M"), MTOK),
        grouped={"model", "context_window"},
    )
    estimate = pricing.estimate_frame(frame)
    assert {GapCode.LONG_CONTEXT_RATE_UNCONFIRMED, GapCode.PRICING_DIMENSION_NOT_GROUPED} <= codes(estimate)
    assert not any(g.invalidates_total for g in estimate.gaps)
    assert _exit_code(frame.with_gaps(estimate.gaps)) == 0


def test_both_gaps_say_what_to_do(pricing):
    frame = frame_of(
        (seg(model="claude-sonnet-4-5", context_window="200k-1M"), MTOK),
        grouped={"model", "context_window"},
    )
    for found in pricing.estimate_frame(frame).gaps:
        assert found.remedy
