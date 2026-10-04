"""A saved Claude Code fetch into its three views. Figures from
tests/_claude_code.py, where each is worked by hand."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from claude_spend_audit.frame import TokenBuckets
from claude_spend_audit.gaps import GapCode
from claude_spend_audit.ingest.api_json import ResponseShapeError
from claude_spend_audit.ingest.claude_code_json import ClaudeCodeFileError, load_claude_code

from . import _claude_code as C


def test_the_frame_holds_only_api_billed_usage():
    """Alice and ci-bot only: carol is a subscriber.

    uncached   alice 1M + 1M + 10M + sonnet 1M, ci-bot 3 x 0.5M  = 14.5M
    cache read alice 3 x 4M                                       = 12M
    writes     alice 3 x 0.2M, TTL unknown                        = 0.6M
    output     alice 0.1M + 0.1M + 1M + sonnet 0.1M, ci-bot 3 x 50k = 1.45M
    """
    data = load_claude_code(C.fetched())
    assert data.frame.tokens == TokenBuckets(
        uncached_input=14_500_000,
        cache_read=12_000_000,
        cache_write_unsplit=600_000,
        output=1_450_000,
    )
    assert data.frame.grouped_by == frozenset({"model"})
    assert data.frame.bucket_width == "1d"
    assert not data.frame.has_token_field("cache_write_1h")
    assert not data.frame.cost


def test_our_estimate_matches_anthropics_on_the_same_rows(pricing):
    data = load_claude_code(C.fetched())
    assert data.api_estimate_usd == Decimal("107.25")
    assert data.total_estimate_usd == Decimal("152.25")
    assert pricing.estimate_frame(data.frame).usd == Decimal("107.25")


def test_the_spend_report_keeps_everyone_and_says_it_is_an_estimate():
    data = load_claude_code(C.fetched())
    report = data.report
    assert report.spend_label == "estimated cost"
    assert report.spend_total == Decimal("152.25")
    assert set(report.distinct("user_email")) == {
        "alice@example.com", "carol@example.com", "api key: ci-bot"
    }
    assert report.prompt_includes_cache is True


def test_activity_records_sessions_and_tool_decisions():
    data = load_claude_code(C.fetched())
    alice = [r for r in data.activity.records if r.actor == "alice@example.com"]
    assert [r.sessions for r in sorted(alice, key=lambda r: r.day)] == [2, 2, 1]
    assert sum(r.tools["edit_tool"][1] for r in alice) == 30
    assert sum(r.estimated_usd for r in alice) == Decimal("102.75")


def test_subscription_share_is_disclosed():
    data = load_claude_code(C.fetched())
    gap = next(g for g in data.gaps if g.code is GapCode.SUBSCRIPTION_NOT_BILLED_PER_TOKEN)
    assert gap.scope == "30%"


def test_a_day_unfinished_at_fetch_time_is_flagged():
    data = load_claude_code(C.fetched(fetched_at=datetime(2026, 8, 5, 18, tzinfo=timezone.utc)))
    gap = next(g for g in data.gaps if g.code is GapCode.CLAUDE_CODE_RECENT_DAY)
    assert gap.scope == "2026-08-05" and gap.invalidates_total


def test_non_usd_and_missing_costs_are_refused():
    with pytest.raises(ResponseShapeError, match="'EUR'"):
        load_claude_code(C.fetched(currency="EUR"))
    env = C.fetched()
    env["days"][0]["pages"][0]["data"][0]["model_breakdown"][0]["estimated_cost"]["amount"] = None
    with pytest.raises(ResponseShapeError, match="null"):
        load_claude_code(env)


def test_a_file_without_days_is_refused():
    env = C.fetched()
    del env["days"]
    with pytest.raises(ClaudeCodeFileError):
        load_claude_code(env)
