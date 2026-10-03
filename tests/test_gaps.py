"""Gap semantics -- above all, the null-versus-not-grouped distinction.

This is the test that guards C7 against fabricating its own headline finding.
"""

from datetime import datetime, timezone

import pytest

from claude_spend_audit import gaps as gapmod
from claude_spend_audit.frame import (
    NOT_GROUPED,
    Frame,
    Segment,
    TokenBuckets,
    UsageRow,
    merge,
)
from claude_spend_audit.gaps import GapCode, Severity
from claude_spend_audit.ingest import load_usage_csv

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 2, tzinfo=timezone.utc)


def _frame(segment: Segment, grouped: set[str]) -> Frame:
    return Frame(
        usage=(UsageRow(start=T0, end=T1, segment=segment, tokens=TokenBuckets(output=1)),),
        grouped_by=frozenset(grouped),
    )


# --------------------------------------------------------------------------
# The distinction C7 depends on
# --------------------------------------------------------------------------


def test_null_in_a_grouped_dimension_means_the_default_workspace():
    frame = _frame(Segment(workspace_id=None), {"workspace_id"})
    assert frame.value_of(frame.usage[0].segment, "workspace_id") is None
    assert frame.has_dimension("workspace_id")


def test_null_in_an_ungrouped_dimension_means_not_broken_out():
    frame = _frame(Segment(workspace_id=None), set())
    assert frame.value_of(frame.usage[0].segment, "workspace_id") is NOT_GROUPED
    assert not frame.has_dimension("workspace_id")


def test_the_two_cases_are_distinguishable():
    """If these ever compare equal, C7 can report un-grouped data as
    unattributed spend -- a fabricated finding, on the check where a
    fabrication does the most damage."""
    grouped = _frame(Segment(workspace_id=None), {"workspace_id"})
    ungrouped = _frame(Segment(workspace_id=None), set())
    assert grouped.value_of(grouped.usage[0].segment, "workspace_id") is not (
        ungrouped.value_of(ungrouped.usage[0].segment, "workspace_id")
    )


def test_ungrouped_workspace_raises_dimension_not_grouped_not_unattributed(
    pricing, usage_profile
):
    """The end-to-end version of the same guarantee.

    Since M2 this gap comes from a check's declared preconditions rather than
    from the parser -- ingest reports facts about the data, the registry owns
    which checks those facts block. The guarantee itself is unchanged: an
    export that was never grouped by workspace must not read as unattributed
    spend.
    """
    from claude_spend_audit.checks import Check, run_checks

    frame = _frame(Segment(model="claude-opus-5"), {"model"})
    needs_workspace = Check(
        id="CX",
        name="Attribution coverage",
        run=lambda f, p, c: [],
        needs_dimensions=frozenset({"workspace_id"}),
    )
    run = run_checks(frame, pricing, checks=[needs_workspace])

    codes = {g.code for g in run.gaps}
    assert GapCode.DIMENSION_NOT_GROUPED in codes
    assert GapCode.UNATTRIBUTED_SPEND not in codes

    gap = next(
        g
        for g in run.gaps
        if g.code is GapCode.DIMENSION_NOT_GROUPED and g.scope == "workspace_id"
    )
    assert "property of the export, not a finding" in gap.message


def test_unknown_dimension_is_a_programming_error():
    frame = _frame(Segment(), {"model"})
    with pytest.raises(KeyError):
        frame.value_of(frame.usage[0].segment, "nonsense")


# --------------------------------------------------------------------------
# Gap bookkeeping
# --------------------------------------------------------------------------


def test_gaps_deduplicate_on_code_and_scope():
    frame = Frame()
    frame = frame.with_gaps([gapmod.priority_tier_present("priority")])
    frame = frame.with_gaps([gapmod.priority_tier_present("priority")])
    assert len(frame.gaps) == 1

    # A different scope is a different gap.
    frame = frame.with_gaps([gapmod.priority_tier_present("priority_on_demand")])
    assert len(frame.gaps) == 2


def test_only_total_invalidating_gaps_say_so():
    assert gapmod.priority_tier_present("priority").invalidates_total is True
    assert gapmod.unknown_model("claude-x").invalidates_total is True
    assert gapmod.code_execution_cost_only().invalidates_total is False
    assert gapmod.no_cost_data().invalidates_total is False
    assert gapmod.bedrock_not_visible(detected=False).invalidates_total is False


def test_detected_bedrock_escalates_to_critical():
    assert gapmod.bedrock_not_visible(detected=False).severity is Severity.INFO
    detected = gapmod.bedrock_not_visible(detected=True)
    assert detected.severity is Severity.CRITICAL
    assert detected.invalidates_total is True


def test_gaps_that_can_be_fixed_say_how():
    for gap in (
        gapmod.dimension_not_grouped("service_tier", "C3"),
        gapmod.granularity_too_coarse("1d", "1h", "C6"),
        gapmod.unknown_model("claude-x"),
        gapmod.provisional_csv_schema("console_usage"),
    ):
        assert gap.remedy, f"{gap.code} offers no remedy"


def test_bedrock_marker_in_the_data_is_detected(tmp_path, usage_profile):
    path = tmp_path / "bedrock.csv"
    path.write_text(
        "start,model,uncached_input_tokens,output_tokens\n"
        "2026-08-01T00:00:00Z,anthropic.claude-opus-5,1000,100\n",
        encoding="utf-8",
    )
    frame, _ = load_usage_csv(path, usage_profile)
    gap = next(g for g in frame.gaps if g.code is GapCode.BEDROCK_NOT_VISIBLE)
    assert gap.severity is Severity.CRITICAL
    assert gap.invalidates_total is True


# --------------------------------------------------------------------------
# merge()
# --------------------------------------------------------------------------


def test_merge_intersects_grouped_dimensions():
    """A dimension is only usable if every contributing source carried it.

    Taking the union would let a check read a dimension off rows that never
    had one.
    """
    a = _frame(Segment(model="m", workspace_id="w"), {"model", "workspace_id"})
    b = _frame(Segment(model="m"), {"model"})
    assert merge(a, b).grouped_by == frozenset({"model"})


def test_merge_carries_gaps_from_both_sides():
    a = Frame().with_gaps([gapmod.no_cost_data()])
    b = Frame().with_gaps([gapmod.code_execution_cost_only()])
    codes = {g.code for g in merge(a, b).gaps}
    assert codes == {GapCode.NO_COST_DATA, GapCode.CODE_EXECUTION_COST_ONLY}


def test_cost_only_frame_does_not_narrow_dimensions(usage_csv, usage_profile, cost_csv, cost_profile):
    """A cost export has no segments, so it must not shrink grouped_by to the
    empty set and silently disable every check."""
    from claude_spend_audit.ingest import load_cost_csv

    usage_frame, _ = load_usage_csv(usage_csv, usage_profile)
    cost_frame, _ = load_cost_csv(cost_csv, cost_profile)
    merged = merge(usage_frame, cost_frame)
    assert merged.grouped_by == usage_frame.grouped_by
    assert merged.cost
    assert merged.usage
