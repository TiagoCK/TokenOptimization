"""CSV and API ingest must produce the same Frame from the same data.

Both normalize into one internal representation, so checks never know which
path the data came from.

That is a claim about behaviour, and this is the test that makes it one. The two
fixtures -- tests/fixtures/csv/usage_console.csv and
tests/fixtures/api/usage_report.json -- carry identical numbers in the two wire
formats. If a future change makes the CSV path drop the 1h cache bucket, or the
API path mis-nest cache_creation, the Frames diverge and this fails.
"""

from claude_spend_audit.frame import DIMENSIONS
from claude_spend_audit.ingest import load_usage_csv, load_usage_json

#: The dimensions the fixtures were exported with. The API response cannot
#: report this itself -- an unrequested dimension and a genuinely-null one both
#: come back as null -- so the caller states it, exactly as an API client would.
FIXTURE_GROUP_BY = (
    "model",
    "workspace_id",
    "api_key_id",
    "service_tier",
    "context_window",
    "inference_geo",
    "speed",
)


def _both(usage_csv, usage_profile, usage_json):
    csv_frame, _ = load_usage_csv(usage_csv, usage_profile)
    api_frame = load_usage_json(usage_json, group_by=FIXTURE_GROUP_BY)
    return csv_frame, api_frame


def test_same_rows_in_the_same_order(usage_csv, usage_profile, usage_json):
    csv_frame, api_frame = _both(usage_csv, usage_profile, usage_json)
    assert len(csv_frame.usage) == len(api_frame.usage) == 4
    for from_csv, from_api in zip(csv_frame.usage, api_frame.usage):
        assert from_csv == from_api


def test_same_segments(usage_csv, usage_profile, usage_json):
    csv_frame, api_frame = _both(usage_csv, usage_profile, usage_json)
    for from_csv, from_api in zip(csv_frame.usage, api_frame.usage):
        assert from_csv.segment == from_api.segment
        for dimension in DIMENSIONS:
            assert getattr(from_csv.segment, dimension) == getattr(
                from_api.segment, dimension
            )


def test_same_token_buckets(usage_csv, usage_profile, usage_json):
    """Including the nested cache_creation flattening, which is the one place
    the two wire formats genuinely differ in shape."""
    csv_frame, api_frame = _both(usage_csv, usage_profile, usage_json)
    assert csv_frame.tokens == api_frame.tokens
    assert csv_frame.tokens.cache_write_5m == api_frame.tokens.cache_write_5m == 400_000
    assert csv_frame.tokens.cache_write_1h == api_frame.tokens.cache_write_1h == 100_000


def test_same_grouped_dimensions(usage_csv, usage_profile, usage_json):
    csv_frame, api_frame = _both(usage_csv, usage_profile, usage_json)
    assert csv_frame.grouped_by == api_frame.grouped_by


def test_same_period_and_granularity(usage_csv, usage_profile, usage_json):
    csv_frame, api_frame = _both(usage_csv, usage_profile, usage_json)
    assert csv_frame.period_start == api_frame.period_start
    assert csv_frame.period_end == api_frame.period_end
    assert csv_frame.bucket_width == api_frame.bucket_width == "1d"


def test_same_web_search_requests(usage_csv, usage_profile, usage_json):
    csv_frame, api_frame = _both(usage_csv, usage_profile, usage_json)
    assert csv_frame.web_search_requests == api_frame.web_search_requests == 50


def test_same_estimated_cost(pricing, usage_csv, usage_profile, usage_json):
    """The property that actually matters: the same data costs the same money
    regardless of which door it came in through."""
    csv_frame, api_frame = _both(usage_csv, usage_profile, usage_json)
    from_csv = pricing.estimate_frame(csv_frame)
    from_api = pricing.estimate_frame(api_frame)
    assert from_csv.usd == from_api.usd
    assert from_csv.priced_tokens == from_api.priced_tokens
    assert from_csv.unpriced_tokens == from_api.unpriced_tokens


def test_same_analysis_blocking_gaps(usage_csv, usage_profile, usage_json):
    """Gaps about the data itself must agree.

    Gaps about the *source* legitimately differ -- only the CSV path can have a
    provisional schema -- so those are excluded.
    """
    csv_frame, api_frame = _both(usage_csv, usage_profile, usage_json)
    source_only = {"provisional_csv_schema", "unmapped_csv_columns"}
    csv_codes = {str(g.code) for g in csv_frame.gaps} - source_only
    api_codes = {str(g.code) for g in api_frame.gaps} - source_only
    assert csv_codes == api_codes


def test_api_path_infers_grouping_conservatively(usage_json):
    """Without an explicit group_by, absence is assumed rather than presence.

    A dimension that is null in every row could be un-grouped or could be
    genuinely null throughout. Reading it as un-grouped suppresses a C7 finding;
    reading it as grouped would invent one. Suppressing is the safe error.
    """
    frame = load_usage_json(usage_json)
    assert "model" in frame.grouped_by  # has values, clearly grouped
    assert "account_id" not in frame.grouped_by  # null everywhere
    assert "service_account_id" not in frame.grouped_by
