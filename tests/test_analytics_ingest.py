"""Claude Enterprise Analytics API: a saved fetch into a Frame and a SpendReport.

Figures are hand-calculated in tests/_analytics.py. The tests that matter
most here are the ones about data the API silently withholds: the 100-group
cap with no remainder row, the refresh watermark, and the 30-day revision
window. Each must surface as a gap rather than as a quietly smaller number.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from claude_spend_audit.enterprise import run_enterprise_checks
from claude_spend_audit.frame import TokenBuckets
from claude_spend_audit.gaps import GapCode
from claude_spend_audit.ingest.analytics_json import AnalyticsFileError, load_analytics
from claude_spend_audit.ingest.api_json import load_usage_json

from . import _analytics as A


def _codes(frame):
    return {g.code for g in frame.gaps}


# -- usage and cost ------------------------------------------------------------


def test_usage_series_reads_like_the_usage_api_plus_product():
    frame, _ = load_analytics(A.fetched())
    assert len(frame.usage) == 20
    assert frame.grouped_by == frozenset(A.GROUP_BY)
    assert frame.bucket_width == "1d"
    assert frame.distinct("product") == ("claude_code", "chat")
    # Ten days of opus (1.0M in, 0.5M 1h writes, 0.2M out) and sonnet
    # (0.5M in, 0.1M out).
    assert frame.tokens == TokenBuckets(
        uncached_input=15_000_000, cache_write_1h=5_000_000, output=3_000_000
    )
    assert frame.sources[0].ingest_path == "analytics"

    # Same rows as the Usage API normaliser produces from the same pages.
    reference = load_usage_json(A.fetched()["series"]["usage"]["pages"], group_by=A.GROUP_BY)
    assert frame.usage == reference.usage


def test_billed_and_list_totals():
    """$16.00 billed and $20.00 list a day, for ten days."""
    frame, _ = load_analytics(A.fetched())
    assert frame.billed_total_usd == Decimal("160")
    assert frame.list_total_usd == Decimal("200")
    basis = next(g for g in frame.gaps if g.code is GapCode.LIST_PRICE_BASIS)
    assert basis.scope == "80.0%"
    assert not basis.invalidates_total


def test_plan_context_replaces_the_usage_api_bedrock_note():
    frame, _ = load_analytics(A.fetched())
    codes = _codes(frame)
    assert GapCode.ANALYTICS_PLAN_CONTEXT in codes
    assert GapCode.BEDROCK_NOT_VISIBLE not in codes


def test_a_clean_fetch_raises_no_freshness_or_truncation_gap():
    frame, _ = load_analytics(A.fetched())
    codes = _codes(frame)
    assert GapCode.ANALYTICS_GROUPS_TRUNCATED not in codes
    assert GapCode.ANALYTICS_INCOMPLETE_TAIL not in codes
    assert GapCode.ANALYTICS_NOT_FINAL not in codes
    assert not any(g.invalidates_total for g in frame.gaps)


# -- what the API withholds ------------------------------------------------------


def test_group_cap_truncation_is_detected_against_the_total():
    """Sonnet missing from two days' grouped buckets:

        2 x (500,000 in + 100,000 out) = 1,200,000 tokens missing
        total 10 x 2,300,000           = 23,000,000 tokens
        1.2M / 23M                     = 5.2%
    """
    frame, _ = load_analytics(A.fetched(drop_sonnet_on=(3, 7)))
    gap = next(g for g in frame.gaps if g.code is GapCode.ANALYTICS_GROUPS_TRUNCATED)
    assert gap.scope == "2 bucket(s)"
    assert "1,200,000 tokens (5.2% of the total)" in gap.message
    assert gap.invalidates_total


def test_a_range_past_the_refresh_watermark_is_incomplete():
    frame, _ = load_analytics(A.fetched(refreshed="2026-08-09T00:00:00Z"))
    gap = next(g for g in frame.gaps if g.code is GapCode.ANALYTICS_INCOMPLETE_TAIL)
    assert gap.scope == "2026-08-09 00:00 UTC"
    assert gap.invalidates_total


def test_a_page_with_no_export_is_incomplete_too():
    frame, _ = load_analytics(A.fetched(refreshed=None))
    assert GapCode.ANALYTICS_INCOMPLETE_TAIL in _codes(frame)


def test_recent_days_are_flagged_as_not_final():
    """Fetched 2026-08-20: all ten days (08-01 to 08-11) are inside 30 days."""
    frame, _ = load_analytics(
        A.fetched(fetched_at=datetime(2026, 8, 20, tzinfo=timezone.utc))
    )
    gap = next(g for g in frame.gaps if g.code is GapCode.ANALYTICS_NOT_FINAL)
    assert gap.scope == "10 day(s)"
    assert not gap.invalidates_total


def test_overlapping_rbac_groups_are_refused():
    with pytest.raises(AnalyticsFileError, match="overlap"):
        load_analytics(A.fetched(group_by=["model", "rbac_group_id"]))


def test_a_fetch_without_usage_is_refused():
    envelope = A.fetched()
    del envelope["series"]["usage"]
    with pytest.raises(AnalyticsFileError, match="no 'usage' series"):
        load_analytics(envelope)


# -- the per-user SpendReport ----------------------------------------------------


def test_per_user_rows_are_summed_across_windows():
    """a@example.com, two windows of 5M in + 2.5M 1h writes + 1M out:

        prompt     2 x (5,000,000 + 2,500,000) = 15,000,000
        completion 2 x 1,000,000               =  2,000,000
        net        2 x $80.00                  =    $160.00
        gross      2 x $100.00                 =    $200.00
    """
    _, report = load_analytics(A.fetched())
    alice = next(r for r in report.rows if r.user_email == "a@example.com")
    assert alice.prompt_tokens == 15_000_000
    assert alice.completion_tokens == 2_000_000
    assert alice.cache_write_tokens == 5_000_000
    assert alice.requests == 100
    assert alice.net_spend_usd == Decimal("160")
    assert alice.gross_spend_usd == Decimal("200")
    assert alice.product == "claude_code" and alice.model == "claude-opus-5"


def test_deleted_users_are_labelled_by_name_and_id():
    """Every deleted user is named "Deleted User"; the id keeps them apart."""
    _, report = load_analytics(A.fetched())
    assert "Deleted User (user_02)" in report.distinct("user_email")


def test_report_knows_what_prompt_tokens_mean():
    _, report = load_analytics(A.fetched())
    assert report.prompt_includes_cache is True
    assert report.spend_field == "net_spend_usd"
    assert report.total("net_spend_usd") == Decimal("170")


def test_no_users_means_no_report():
    _, report = load_analytics(A.fetched(users=False))
    assert report is None


def test_e_checks_run_on_api_data(pricing):
    """Net: a@example.com $160, the deleted user $10. Top 1 of 2 = 94%."""
    _, report = load_analytics(A.fetched())
    run = run_enterprise_checks(report, pricing)
    e1 = next(f for f in run.findings if f.check_id == "E1")
    assert e1.measured_usd == Decimal("160")
    assert "94% of net spend" in e1.summary
    assert {f.check_id for f in run.findings} == {"E1", "E2", "E3"}
    # Neither segment's output reaches 25% of tokens (2/17 and 1/6).
    assert "Output token share (E4)" in run.clean
