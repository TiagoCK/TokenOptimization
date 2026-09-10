"""Schema matching: fail loudly, never guess.

Anthropic does not publish the Console export's column headers, so this layer is
the project's largest unverified assumption. Its value depends entirely on
behaving well when it is wrong.
"""

import pytest

from claude_spend_audit.ingest.schema import (
    Profile,
    SchemaError,
    SchemaMismatch,
    infer_bucket_width,
    normalize,
    parse_datetime,
)
from claude_spend_audit.ingest import load_usage_csv

MINIMAL = {
    "profile": "test",
    "kind": "usage",
    "status": "provisional",
    "columns": {
        "start": {"type": "datetime", "required": True, "aliases": ["date"]},
        "output": {"type": "int", "required": True, "aliases": ["output tokens"]},
        "model": {"type": "nullable_string", "dimension": True, "aliases": ["model"]},
    },
    "null_tokens": [""],
}


def test_normalization_folds_case_and_separators():
    assert normalize("Uncached_Input-Tokens") == "uncached input tokens"
    assert normalize("  OUTPUT   TOKENS ") == "output tokens"
    assert normalize("cache_creation.ephemeral_5m_input_tokens") == (
        "cache creation ephemeral 5m input tokens"
    )


def test_headers_match_regardless_of_spelling_style():
    profile = Profile.from_dict(MINIMAL)
    mapping = profile.match(("Date", "Output Tokens", "MODEL"))
    assert mapping.fields == {"start": 0, "output": 1, "model": 2}
    assert mapping.unmapped == ()


def test_unrecognised_columns_are_reported_not_dropped():
    profile = Profile.from_dict(MINIMAL)
    mapping = profile.match(("date", "output tokens", "mystery column"))
    assert mapping.unmapped == ("mystery column",)


def test_missing_required_column_raises_with_the_real_header_row():
    """The error has to be actionable in one pass: what we wanted, what was
    actually there, and which file to edit."""
    profile = Profile.from_dict(MINIMAL)
    with pytest.raises(SchemaMismatch) as excinfo:
        profile.match(("date", "completion_count", "widget"))

    message = str(excinfo.value)
    assert "output" in message  # the field we could not find
    assert "'completion_count'" in message  # the headers actually present
    assert "'widget'" in message
    assert "aliases" in message  # how to fix it
    assert excinfo.value.missing == ("output",)
    assert excinfo.value.headers == ("date", "completion_count", "widget")


def test_ambiguous_match_is_an_error_not_a_coin_flip():
    """Two columns claiming one field means the export is not what we think.

    Picking one would silently discard data.
    """
    profile = Profile.from_dict(MINIMAL)
    with pytest.raises(SchemaMismatch, match="Ambiguous"):
        profile.match(("date", "output", "output tokens"))


def test_a_profile_with_colliding_aliases_is_rejected():
    """Catch the mistake in the profile itself, not in the data."""
    broken = {
        "profile": "broken",
        "kind": "usage",
        "columns": {
            "a": {"type": "int", "aliases": ["tokens"]},
            "b": {"type": "int", "aliases": ["tokens"]},
        },
    }
    profile = Profile.from_dict(broken)
    with pytest.raises(SchemaError, match="claimed by"):
        profile.match(("tokens",))


def test_profile_without_columns_is_rejected():
    with pytest.raises(SchemaError, match="no columns"):
        Profile.from_dict({"profile": "empty", "columns": {}})


def test_shipped_profiles_are_marked_provisional(usage_profile, cost_profile):
    """A deliberate reminder, not an oversight. Flip to 'verified' only once a
    real Console export has confirmed the headers."""
    assert usage_profile.status == "provisional"
    assert cost_profile.status == "provisional"
    assert usage_profile.verified_against is None
    assert cost_profile.verified_against is None


def test_cost_profile_states_its_amount_units(cost_profile):
    """Whether the export writes cents or dollars is a 100x question, so the
    assumption is explicit and inspectable rather than buried in the parser."""
    assert cost_profile.amount_units == "cents"


# --------------------------------------------------------------------------
# Value parsing
# --------------------------------------------------------------------------


def test_naive_timestamps_are_read_as_utc():
    """The API snaps every bucket to a UTC boundary. Reading a naive timestamp
    as local time would shift every bucket and corrupt C6's off-hours logic."""
    parsed = parse_datetime("2026-08-01 00:00:00")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_offset_timestamps_are_converted_to_utc():
    parsed = parse_datetime("2026-08-01T05:00:00+05:00")
    assert parsed.hour == 0
    assert parsed.utcoffset().total_seconds() == 0


def test_thousands_separators_survive():
    """Exports opened and re-saved in a spreadsheet routinely acquire these."""
    profile = Profile.from_dict(MINIMAL)
    spec = profile.columns["output"]
    assert profile.parse(spec, "1,234,567", row_no=2) == 1234567


def test_unparseable_value_raises_with_the_row_number():
    profile = Profile.from_dict(MINIMAL)
    spec = profile.columns["output"]
    with pytest.raises(SchemaMismatch, match="Row 7"):
        profile.parse(spec, "not a number", row_no=7)


def test_bucket_width_inference():
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    hourly = [base + timedelta(hours=i) for i in range(5)]
    daily = [base + timedelta(days=i) for i in range(5)]
    assert infer_bucket_width(hourly) == "1h"
    assert infer_bucket_width(daily) == "1d"


def test_bucket_width_is_none_when_it_cannot_be_known():
    """Guessing here would let an off-hours check run on daily data."""
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert infer_bucket_width([base]) is None
    assert infer_bucket_width([]) is None
    assert infer_bucket_width([base, base + timedelta(seconds=90)]) is None


# --------------------------------------------------------------------------
# End to end on a wrong file
# --------------------------------------------------------------------------


def test_a_completely_wrong_csv_fails_usefully(tmp_path, usage_profile):
    wrong = tmp_path / "sales.csv"
    wrong.write_text("customer,region,revenue\nAcme,EMEA,1000\n", encoding="utf-8")

    with pytest.raises(SchemaMismatch) as excinfo:
        load_usage_csv(wrong, usage_profile)

    message = str(excinfo.value)
    assert "'customer'" in message
    assert "'revenue'" in message
    assert "console_usage" in message


def test_a_bom_does_not_break_the_first_column(tmp_path, usage_profile):
    """Excel adds one on save; it would otherwise corrupt the first header."""
    path = tmp_path / "bom.csv"
    path.write_text(
        "start,uncached_input_tokens,output_tokens\n"
        "2026-08-01T00:00:00Z,1000,100\n",
        encoding="utf-8-sig",
    )
    frame, mapping = load_usage_csv(path, usage_profile)
    assert "start" in mapping.fields
    assert len(frame.usage) == 1


def test_missing_profile_directory_explains_the_fix(tmp_path):
    """Rates and profiles live at the repo root so they stay editable, which
    means a non-editable `pip install .` cannot find them. Fail with the
    remedy attached rather than a bare path."""
    with pytest.raises(SchemaError) as excinfo:
        Profile.load_named("console_usage_csv", tmp_path)
    message = str(excinfo.value)
    assert "pip install -e ." in message
    assert "--usage-profile" in message


def test_missing_pricing_directory_explains_the_fix(tmp_path):
    from claude_spend_audit.pricing import PricingError, PricingTable

    with pytest.raises(PricingError) as excinfo:
        PricingTable.load_default(tmp_path)
    message = str(excinfo.value)
    assert "pip install -e ." in message
    assert "--pricing" in message
