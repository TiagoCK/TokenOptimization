"""C7r -- RBAC group coverage, with figures by hand.

Per day (tests/_analytics.py): rbac_group_eng $12.00, rbac_group_sales $3.00,
users in no group $4.00, against $16.00 billed. Over ten days:

    unassigned  10 x $4.00  = $40.00
    total       10 x $16.00 = $160.00     ->  25%

The named rows come to $150.00 and, with the null row, $190.00 -- more than
the total, because a user in two groups counts in both. The test that matters
most proves that overlap never reaches a figure.
"""

from decimal import Decimal

from claude_spend_audit.checks import CheckConfig
from claude_spend_audit.enterprise.groups import run_group_coverage
from claude_spend_audit.findings import Confidence
from claude_spend_audit.ingest.analytics_json import GroupCoverage, load_analytics

from . import _analytics as A


def _coverage(**kw) -> GroupCoverage:
    _, _, coverage = load_analytics(A.fetched(**kw))
    return coverage


def test_unassigned_share_by_hand():
    [finding] = run_group_coverage(_coverage(), CheckConfig())
    assert finding.check_id == "C7r"
    assert finding.measured_usd == Decimal("40")
    assert finding.scope == "25% of billed spend"
    assert "25% of billed spend ($40.00 of $160.00)" in finding.summary


def test_named_groups_are_shown_but_never_summed():
    [finding] = run_group_coverage(_coverage(), CheckConfig())
    rows = {row[0]: row[1] for row in finding.evidence.rows}
    assert rows["in no group"] == "$40.00"
    assert rows["account total"] == "$160.00"
    assert rows["rbac_group_eng"] == "$120.00"
    assert rows["rbac_group_sales"] == "$30.00"
    # No row anywhere carries the $190.00 an overlapping sum would give.
    assert "$190.00" not in str(finding)
    assert "overlap" in finding.evidence.note


def test_an_account_with_no_named_groups_says_so():
    coverage = GroupCoverage(total_usd=Decimal("100"), unassigned_usd=Decimal("100"))
    [finding] = run_group_coverage(coverage, CheckConfig())
    assert "No named group appears at all" in finding.summary
    assert finding.scope == "100% of billed spend"


def test_a_capped_bucket_makes_the_figure_a_lower_bound():
    """Day 4 carries 100 named rows and no null row: $36.00 of nine days only."""
    coverage = _coverage(groups_capped_on=(4,))
    assert coverage.capped_buckets == 1
    assert coverage.unassigned_usd == Decimal("36")
    [finding] = run_group_coverage(coverage, CheckConfig())
    assert any("lower bound" in c for c in finding.caveats)


def test_a_small_unassigned_share_is_not_reported():
    """$5.00 of $160.00 is 3%, under the 10% threshold C7 also uses."""
    coverage = GroupCoverage(total_usd=Decimal("160"), unassigned_usd=Decimal("5"))
    assert run_group_coverage(coverage, CheckConfig()) == []


def test_min_impact_applies():
    coverage = _coverage()
    assert run_group_coverage(coverage, CheckConfig(min_impact_usd=Decimal("50"))) == []


def test_the_finding_claims_no_saving():
    [finding] = run_group_coverage(_coverage(), CheckConfig())
    assert finding.confidence is Confidence.OBSERVATION
    assert finding.recoverable is None
    assert "billed spend" in finding.measured_basis
