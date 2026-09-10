"""C7 -- attribution coverage.

This is the check that `Frame.grouped_by` was designed to protect, so the most
important test here is the negative one: an export that was never broken out by
workspace must produce a gap, never a 100%-unattributed finding.
"""

from datetime import datetime, timezone
from decimal import Decimal

from claude_spend_audit.checks import CheckConfig, run_checks
from claude_spend_audit.checks.c7_attribution import CHECK as C7
from claude_spend_audit.findings import Confidence
from claude_spend_audit.frame import Frame, Segment, TokenBuckets, UsageRow
from claude_spend_audit.gaps import GapCode
from claude_spend_audit.ingest import load_usage_csv

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 2, tzinfo=timezone.utc)

#: 1,000,000 uncached input on claude-opus-5 = $5.00 exactly.
FIVE_DOLLARS = TokenBuckets(uncached_input=1_000_000)


def seg(**kw) -> Segment:
    base = dict(
        model="claude-opus-5",
        workspace_id="wrkspc_named",
        api_key_id="apikey_named",
        service_tier="standard",
        inference_geo="global",
        speed="standard",
    )
    return Segment(**(base | kw))


def frame_of(*segments: Segment, grouped: set[str] | None = None) -> Frame:
    return Frame(
        usage=tuple(
            UsageRow(start=T0, end=T1, segment=s, tokens=FIVE_DOLLARS) for s in segments
        ),
        grouped_by=frozenset(
            grouped or {"model", "workspace_id", "api_key_id", "service_tier"}
        ),
        bucket_width="1d",
    )


def only(findings):
    assert len(findings) == 1, f"expected one finding, got {len(findings)}"
    return findings[0]


# --------------------------------------------------------------------------
# The invariant this check exists to respect
# --------------------------------------------------------------------------


def test_ungrouped_workspace_blocks_the_check(pricing, usage_profile, tmp_path):
    """An export not broken out by workspace must NOT read as 100%
    unattributed. This is the fabricated finding the design prevents."""
    path = tmp_path / "no_workspace.csv"
    path.write_text(
        "start,model,uncached_input_tokens,output_tokens\n"
        "2026-08-01T00:00:00Z,claude-opus-5,1000000,100000\n",
        encoding="utf-8",
    )
    frame, _ = load_usage_csv(path, usage_profile)
    assert not frame.has_dimension("workspace_id")

    unmet = C7.unmet(frame)
    assert len(unmet) == 1
    assert unmet[0].code is GapCode.DIMENSION_NOT_GROUPED
    assert "Attribution coverage (C7)" in unmet[0].message

    run = run_checks(frame, pricing, CheckConfig())
    assert not [f for f in run.findings if f.check_id == "C7"]
    assert GapCode.UNATTRIBUTED_SPEND not in {g.code for g in run.gaps}


def test_null_workspace_in_a_grouped_export_is_a_real_finding(pricing):
    """The other side of the same distinction: here the export *was* grouped,
    so a null genuinely means the default workspace.

        $5.00 unattributed of $10.00 total = 50%
    """
    finding = only(
        list(
            C7.run(
                frame_of(seg(), seg(workspace_id=None)), pricing, CheckConfig()
            )
        )
    )
    assert finding.measured_usd == Decimal("5.00")
    assert "50%" in finding.summary


# --------------------------------------------------------------------------
# Reporting the causes separately
# --------------------------------------------------------------------------


def test_causes_are_reported_separately(pricing):
    """Default-workspace and playground spend are different problems.

        $5.00 default workspace (named key)
        $5.00 playground (named workspace, no key)
        $5.00 attributable
        ---------------------------------------
        $15.00 total, $10.00 unattributed = 67%
    """
    finding = only(
        list(
            C7.run(
                frame_of(seg(), seg(workspace_id=None), seg(api_key_id=None)),
                pricing,
                CheckConfig(),
            )
        )
    )
    assert finding.measured_usd == Decimal("10.00")
    labels = [row[0] for row in finding.evidence.rows]
    assert "default workspace (named key)" in labels
    assert "Console playground (no API key)" in labels


def test_buckets_are_mutually_exclusive_and_sum_to_the_total(pricing):
    """A segment missing *both* must be counted once, not in two rows.

    Overlapping buckets sum to more than 100% and read as double counting.

        $5.00 both null  +  $5.00 attributable  =  $10.00 total
    """
    finding = only(
        list(
            C7.run(
                frame_of(seg(), seg(workspace_id=None, api_key_id=None)),
                pricing,
                CheckConfig(),
            )
        )
    )
    assert finding.measured_usd == Decimal("5.00")

    amounts = [Decimal(row[1].lstrip("$").replace(",", "")) for row in finding.evidence.rows]
    assert sum(amounts) == Decimal("10.00")
    shares = [int(row[2].rstrip("%")) for row in finding.evidence.rows]
    assert sum(shares) == 100


# --------------------------------------------------------------------------
# It is an observation, so it claims nothing
# --------------------------------------------------------------------------


def test_claims_no_savings(pricing):
    finding = only(
        list(C7.run(frame_of(seg(), seg(workspace_id=None)), pricing, CheckConfig()))
    )
    assert finding.confidence is Confidence.OBSERVATION
    assert finding.recoverable is None
    assert "not waste" in finding.summary


def test_says_default_workspace_spend_is_attributable_in_principle(pricing):
    """An engineer whose main workspace IS the default one would otherwise
    reject the finding outright, and fairly."""
    finding = only(
        list(C7.run(frame_of(seg(), seg(workspace_id=None)), pricing, CheckConfig()))
    )
    assert any("attributable in principle" in c for c in finding.caveats)


# --------------------------------------------------------------------------
# Staying quiet
# --------------------------------------------------------------------------


def test_fully_attributed_spend_produces_nothing(pricing):
    assert list(C7.run(frame_of(seg(), seg()), pricing, CheckConfig())) == []


def test_below_the_share_threshold_produces_nothing(pricing):
    """One unattributed segment in twenty is 5%, under the 10% default."""
    segments = [seg() for _ in range(19)] + [seg(workspace_id=None)]
    assert list(C7.run(frame_of(*segments), pricing, CheckConfig())) == []


def test_against_the_fixture(pricing, usage_profile, usage_csv):
    """The shipped fixture's sonnet row has both a null workspace and a null
    key: $4.50 of $27.25 priced spend = 17%."""
    frame, _ = load_usage_csv(usage_csv, usage_profile)
    finding = only(list(C7.run(frame, pricing, CheckConfig())))
    assert finding.measured_usd == Decimal("4.50")
    assert "17%" in finding.summary
    # Both nulls on one segment -> one exclusive row, plus the attributable row.
    assert len(finding.evidence.rows) == 2
