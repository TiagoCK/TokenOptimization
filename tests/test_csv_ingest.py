"""Golden-file tests for the CSV ingest path."""

from datetime import datetime, timezone
from decimal import Decimal

from claude_spend_audit.frame import NOT_GROUPED
from claude_spend_audit.gaps import GapCode
from claude_spend_audit.ingest import load_cost_csv, load_usage_csv


def test_usage_rows_land_intact(usage_csv, usage_profile):
    frame, _ = load_usage_csv(usage_csv, usage_profile)
    assert len(frame.usage) == 4

    row = frame.usage[0]
    assert row.start == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert row.end == datetime(2026, 8, 2, tzinfo=timezone.utc)
    assert row.segment.model == "claude-opus-5"
    assert row.segment.service_tier == "standard"
    assert row.segment.inference_geo == "global"
    assert row.tokens.uncached_input == 1_000_000
    assert row.tokens.cache_read == 2_000_000
    assert row.tokens.cache_write_5m == 400_000
    assert row.tokens.cache_write_1h == 100_000
    assert row.tokens.output == 200_000
    assert row.web_search_requests == 50


def test_cache_ttls_stay_separate(usage_csv, usage_profile):
    """The whole point of five buckets rather than four."""
    frame, _ = load_usage_csv(usage_csv, usage_profile)
    tokens = frame.tokens
    assert tokens.cache_write_5m == 400_000
    assert tokens.cache_write_1h == 100_000
    assert tokens.cache_write_total == 500_000


def test_token_totals(usage_csv, usage_profile):
    #   uncached  1,000,000 + 2,000,000 + 1,000,000 + 5,000,000 = 9,000,000
    #   read      2,000,000
    #   5m write    400,000
    #   1h write    100,000
    #   output      200,000 +   500,000 +   100,000 + 1,000,000 = 1,800,000
    #                                                            ----------
    #                                                            13,300,000
    frame, _ = load_usage_csv(usage_csv, usage_profile)
    tokens = frame.tokens
    assert tokens.uncached_input == 9_000_000
    assert tokens.cache_read == 2_000_000
    assert tokens.output == 1_800_000
    assert tokens.total == 13_300_000
    assert frame.web_search_requests == 50


def test_blank_workspace_reads_as_the_default_workspace(usage_csv, usage_profile):
    """An empty cell is the API's null, not a parse failure.

    §2: workspace_id is null for the default workspace and api_key_id is null
    for Console playground traffic. Neither is an error.
    """
    frame, _ = load_usage_csv(usage_csv, usage_profile)
    sonnet = next(r for r in frame.usage if r.segment.model == "claude-sonnet-5")
    assert sonnet.segment.workspace_id is None
    assert sonnet.segment.api_key_id is None
    # And it is a real null, not an un-grouped dimension:
    assert frame.value_of(sonnet.segment, "workspace_id") is None
    assert frame.value_of(sonnet.segment, "workspace_id") is not NOT_GROUPED


def test_bucket_width_inferred_from_spacing(usage_csv, usage_profile):
    frame, _ = load_usage_csv(usage_csv, usage_profile)
    assert frame.bucket_width == "1d"


def test_period_bounds(usage_csv, usage_profile):
    frame, _ = load_usage_csv(usage_csv, usage_profile)
    assert frame.period_start == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert frame.period_end == datetime(2026, 8, 3, tzinfo=timezone.utc)


def test_grouped_dimensions_come_from_the_columns_present(usage_csv, usage_profile):
    frame, _ = load_usage_csv(usage_csv, usage_profile)
    assert frame.grouped_by == frozenset(
        {
            "model",
            "workspace_id",
            "api_key_id",
            "service_tier",
            "context_window",
            "inference_geo",
            "speed",
        }
    )
    # Columns the export does not carry are not claimed as grouped.
    assert "account_id" not in frame.grouped_by


def test_distinct_values(usage_csv, usage_profile):
    frame, _ = load_usage_csv(usage_csv, usage_profile)
    assert set(frame.distinct("model")) == {
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    }
    assert set(frame.distinct("service_tier")) == {"standard", "batch", "priority"}


# --------------------------------------------------------------------------
# Cost path
# --------------------------------------------------------------------------


def test_cost_amounts_convert_from_cents(cost_csv, cost_profile):
    """"500" in the export means $5.00, not $500.00.

    A factor-of-100 error here would be the single most damaging bug this tool
    could ship.
    """
    frame, _ = load_cost_csv(cost_csv, cost_profile)
    first = frame.cost[0]
    assert first.amount_usd == Decimal("5")
    assert first.model == "claude-opus-5"
    assert first.token_type == "uncached_input_tokens"


def test_billed_total(cost_csv, cost_profile):
    #   5.00 + 1.00 + 2.50 + 1.00 + 5.00   (opus 5, day 1)      = 14.50
    # + 2.00 + 2.50                        (sonnet 5, batch)    =  4.50
    # + 0.50                               (web search)         =  0.50
    # + 3.00                               (code execution)     =  3.00
    # + 5.50 + 2.75                        (opus 5 us, day 2)   =  8.25
    #                                                            ------
    #                                                             30.75
    frame, _ = load_cost_csv(cost_csv, cost_profile)
    assert frame.billed_total_usd == Decimal("30.75")


def test_cost_types_are_preserved(cost_csv, cost_profile):
    frame, _ = load_cost_csv(cost_csv, cost_profile)
    types = {r.cost_type for r in frame.cost}
    assert types == {"tokens", "web_search", "code_execution"}


def test_all_five_token_types_appear_in_cost(cost_csv, cost_profile):
    frame, _ = load_cost_csv(cost_csv, cost_profile)
    token_types = {r.token_type for r in frame.cost if r.cost_type == "tokens"}
    assert token_types == {
        "uncached_input_tokens",
        "cache_read_input_tokens",
        "cache_creation.ephemeral_5m_input_tokens",
        "cache_creation.ephemeral_1h_input_tokens",
        "output_tokens",
    }


# --------------------------------------------------------------------------
# Gaps raised at ingest
# --------------------------------------------------------------------------


def test_provisional_schema_is_flagged(usage_csv, usage_profile):
    """Until a real export verifies the headers, say so on every run."""
    frame, _ = load_usage_csv(usage_csv, usage_profile)
    assert any(g.code is GapCode.PROVISIONAL_CSV_SCHEMA for g in frame.gaps)


def test_priority_tier_in_the_data_raises_a_critical_gap(usage_csv, usage_profile):
    frame, _ = load_usage_csv(usage_csv, usage_profile)
    gap = next(g for g in frame.gaps if g.code is GapCode.PRIORITY_TIER_PRESENT)
    assert gap.invalidates_total is True
    assert gap.scope == "priority"


def test_bedrock_caveat_is_present_but_makes_no_claim(usage_csv, usage_profile):
    """No positive Bedrock signal exists in this data, so none is claimed."""
    frame, _ = load_usage_csv(usage_csv, usage_profile)
    gap = next(g for g in frame.gaps if g.code is GapCode.BEDROCK_NOT_VISIBLE)
    assert gap.invalidates_total is False
    assert "no signal in aggregate data" in gap.message


def test_daily_granularity_blocks_c6(usage_csv, usage_profile):
    frame, _ = load_usage_csv(usage_csv, usage_profile)
    gap = next(g for g in frame.gaps if g.code is GapCode.GRANULARITY_TOO_COARSE)
    assert gap.scope == "1d"
    assert "1h" in gap.remedy
