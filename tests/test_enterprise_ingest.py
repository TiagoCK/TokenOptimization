"""Team/Enterprise spend report ingest: many aliases, and still no guessing.

No genuine export of this report has been seen. Anthropic's support article
(12883420) names five columns verbatim and describes the rest in prose, so the
profile carries many candidate spellings per field. That only stays honest if
the old rules survive the extra aliases:

* no alias may be claimed by two fields, or a header could land in either
* a missing required column fails with the real header row printed
* two columns claiming one field is an error, never a coin flip
* a column the export lacks is None, never 0

And it must never steal a Console or cookbook export from the usage path.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from claude_spend_audit.gaps import GapCode
from claude_spend_audit.ingest.enterprise_csv import (
    ENTERPRISE_KIND,
    detect_enterprise,
    load_enterprise_csv,
)
from claude_spend_audit.ingest.schema import (
    Profile,
    SchemaMismatch,
    normalize,
    select_profile,
)

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "schemas" / "enterprise_spend_csv.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "csv"
OFFICIAL = FIXTURES / "enterprise_spend.csv"
VARIANT = FIXTURES / "enterprise_spend_variant.csv"
MINIMAL = FIXTURES / "enterprise_spend_minimal.csv"

#: The header row as support article 12883420 describes it. The five
#: total_* names are verbatim; the first five are its prose ("the user's
#: email, account UUID, product, model and model family") in snake case.
ARTICLE_HEADERS = (
    "user_email",
    "account_uuid",
    "product",
    "model",
    "model_family",
    "total_requests",
    "total_prompt_tokens",
    "total_completion_tokens",
    "total_net_spend_usd",
    "total_gross_spend_usd",
)


@pytest.fixture(scope="module")
def profile() -> Profile:
    return Profile.load(PROFILE_PATH)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "spend.csv"
    path.write_text(text, encoding="utf-8")
    return path


# -- the profile itself ------------------------------------------------------


def test_profile_is_provisional_and_its_own_kind(profile):
    assert profile.kind == ENTERPRISE_KIND
    assert profile.status == "provisional"
    assert {name for name, spec in profile.columns.items() if spec.required} == {
        "user_email",
        "model",
    }


def test_no_alias_is_claimed_by_two_fields(profile):
    """With this many guesses, a collision is the likeliest mistake.

    Profile.match also raises on one, but only when a match is attempted; this
    names the offending alias directly.
    """
    owner: dict[str, str] = {}
    for spec in profile.columns.values():
        for alias in (spec.name, *spec.aliases):
            key = normalize(alias)
            assert owner.setdefault(key, spec.name) == spec.name, (
                f"alias {alias!r} is claimed by both {owner[key]!r} and {spec.name!r}"
            )


def test_every_field_has_several_aliases(profile):
    thin = [
        name
        for name, spec in profile.columns.items()
        if len(spec.aliases) < 2 and name != "currency"
    ]
    assert thin == []


def test_the_article_header_row_matches(profile):
    chosen = select_profile(ARTICLE_HEADERS, ENTERPRISE_KIND)
    assert chosen.name == "enterprise_spend"
    mapping = chosen.match(ARTICLE_HEADERS)
    assert mapping.unmapped == ()
    assert set(mapping.fields) == {
        "user_email",
        "account_uuid",
        "product",
        "model",
        "model_family",
        "requests",
        "prompt_tokens",
        "completion_tokens",
        "net_spend_usd",
        "gross_spend_usd",
    }


@pytest.mark.parametrize(
    "header, field",
    [
        ("Email", "user_email"),
        ("E-mail", "user_email"),
        ("Member Email", "user_email"),
        ("User ID", "account_uuid"),
        ("Product Name", "product"),
        ("Surface", "product"),
        ("Model Name", "model"),
        ("Model Family", "model_family"),
        ("Total Requests", "requests"),
        ("Messages", "requests"),
        ("Input Tokens", "prompt_tokens"),
        ("Total Prompt Tokens", "prompt_tokens"),
        ("Output Tokens", "completion_tokens"),
        ("total_cache_read_tokens", "cache_read_tokens"),
        ("Cache Creation Tokens", "cache_write_tokens"),
        ("total_web_search_count", "web_search_count"),
        ("Net Spend (USD)", "net_spend_usd"),
        ("Spend", "net_spend_usd"),
        ("Total Cost", "net_spend_usd"),
        ("Gross Spend (USD)", "gross_spend_usd"),
        ("total_gross_spend_usd", "gross_spend_usd"),
        ("Start Date", "period_start"),
        ("End Date", "period_end"),
    ],
)
def test_alternative_spellings_map_to_the_right_field(profile, header, field):
    base = [h for h, f in (("user_email", "user_email"), ("model", "model")) if f != field]
    mapping = profile.match(tuple(base) + (header,))
    assert mapping.index_of(field) == len(base)


# -- loading -------------------------------------------------------------------


def test_official_fixture_loads(pricing):
    """Totals, from the fixture:

        net    600 + 40 + 200 + 120 + 8 + 12 + 16 + 4 = $1,000.00
        gross  750 + 50 + 250 + 150 + 10 + 15 + 20 + 5 = $1,250.00
    """
    report, mapping = load_enterprise_csv(OFFICIAL)
    assert len(report.rows) == 8
    assert report.total("net_spend_usd") == Decimal("1000.00")
    assert report.total("gross_spend_usd") == Decimal("1250.00")
    assert report.total("requests") == 1140
    assert report.spend_field == "net_spend_usd"
    assert report.distinct("user_email") == (
        "alice@example.com",
        "bob@example.com",
        "carol@example.com",
        "dan@example.com",
    )
    first = report.rows[0]
    assert first.product == "Claude Code"
    assert first.prompt_tokens == 8_000_000
    assert first.net_spend_usd == Decimal("600.00")
    # Columns the export does not carry are absent, not zero.
    assert first.cache_read_tokens is None
    assert report.total("cache_read_tokens") is None


def test_variant_spellings_bom_and_formatted_numbers():
    """BOM, title-case headers, thousands separators and dollar signs."""
    report, mapping = load_enterprise_csv(VARIANT)
    assert mapping.unmapped == ("Team",)
    erin = report.rows[0]
    assert erin.user_email == "erin@example.com"
    assert erin.requests == 1200
    assert erin.prompt_tokens == 40_000_000
    assert erin.net_spend_usd == Decimal("1234.50")
    assert erin.gross_spend_usd == Decimal("1543.13")
    codes = {g.code for g in report.gaps}
    assert GapCode.UNMAPPED_CSV_COLUMNS in codes


def test_source_gaps_name_the_right_export():
    report, _ = load_enterprise_csv(OFFICIAL)
    codes = {g.code for g in report.gaps}
    assert GapCode.ENTERPRISE_ANALYTICS_SOURCE in codes
    assert GapCode.PROVISIONAL_CSV_SCHEMA in codes
    provisional = next(g for g in report.gaps if g.code is GapCode.PROVISIONAL_CSV_SCHEMA)
    assert "Team/Enterprise spend report" in provisional.message
    assert "Console" not in provisional.message
    # Nothing here means a total understates the bill: spend is read, not estimated.
    assert not any(g.invalidates_total for g in report.gaps)


def test_blank_cell_in_a_present_column_is_zero_but_absent_column_is_none(tmp_path):
    path = _write(
        tmp_path,
        "email,model,spend,total_prompt_tokens\n"
        "x@example.com,claude-opus-5,10.00,\n",
    )
    report, _ = load_enterprise_csv(path)
    row = report.rows[0]
    assert row.prompt_tokens == 0
    assert row.completion_tokens is None
    assert report.total("prompt_tokens") == 0
    assert report.total("completion_tokens") is None


def test_missing_required_column_prints_the_real_header_row(tmp_path, profile):
    path = _write(tmp_path, "model,spend\nclaude-opus-5,10.00\n")
    with pytest.raises(SchemaMismatch) as caught:
        load_enterprise_csv(path, profile)
    message = str(caught.value)
    assert "user_email" in message
    assert "'model'" in message and "'spend'" in message
    assert "aliases" in message


def test_a_file_with_no_numbers_is_not_a_spend_report(tmp_path):
    path = _write(tmp_path, "email,model\nx@example.com,claude-opus-5\n")
    with pytest.raises(SchemaMismatch) as caught:
        load_enterprise_csv(path)
    assert "no token or spend column" in str(caught.value)


def test_generic_and_net_spend_together_is_ambiguous(tmp_path):
    """Generic spellings map to net. Both present is a refusal, not a pick."""
    path = _write(
        tmp_path,
        "email,model,spend,net spend\nx@example.com,claude-opus-5,10.00,9.00\n",
    )
    with pytest.raises(SchemaMismatch) as caught:
        load_enterprise_csv(path)
    assert "ambiguous" in str(caught.value).lower()

    # Routing still claims it, so the user gets the error that names the two
    # columns rather than a usage-profile mismatch.
    profile = detect_enterprise(path)
    assert profile is not None
    with pytest.raises(SchemaMismatch) as caught:
        load_enterprise_csv(path, profile)
    message = str(caught.value)
    assert "'spend'" in message and "'net spend'" in message
    assert "net_spend_usd" in message


def test_a_totals_row_is_excluded_and_reported(tmp_path):
    """A row naming no one and no model would double the spend if counted."""
    path = _write(
        tmp_path,
        "email,model,spend\n"
        "x@example.com,claude-opus-5,10.00\n"
        "y@example.com,claude-sonnet-5,5.00\n"
        ",,15.00\n",
    )
    report, _ = load_enterprise_csv(path)
    assert len(report.rows) == 2
    assert report.spend_total == Decimal("15.00")
    excluded = [g for g in report.gaps if g.code is GapCode.ENTERPRISE_ROWS_EXCLUDED]
    assert len(excluded) == 1 and excluded[0].scope == "1 row(s)"


def test_period_columns_are_read_when_present(tmp_path):
    path = _write(
        tmp_path,
        "email,model,spend,start date,end date\n"
        "x@example.com,claude-opus-5,10.00,2026-09-01,2026-10-01\n",
    )
    report, _ = load_enterprise_csv(path)
    assert report.period_start.date().isoformat() == "2026-09-01"
    assert report.period_end.date().isoformat() == "2026-10-01"


# -- routing -------------------------------------------------------------------


@pytest.mark.parametrize("fixture", [OFFICIAL, VARIANT, MINIMAL])
def test_spend_reports_are_detected(fixture):
    detected = detect_enterprise(fixture)
    assert detected is not None and detected.name == "enterprise_spend"


@pytest.mark.parametrize(
    "path",
    [
        FIXTURES / "usage_console.csv",
        FIXTURES / "cookbook_usage.csv",
        FIXTURES / "usage_hourly.csv",
        FIXTURES / "usage_cache_waste.csv",
        ROOT / "examples" / "sample-usage.csv",
    ],
)
def test_usage_exports_are_never_claimed(path):
    assert detect_enterprise(path) is None


def test_the_spend_report_never_matches_a_usage_profile():
    with pytest.raises(SchemaMismatch):
        select_profile(ARTICLE_HEADERS, "usage")


def test_unreadable_or_missing_files_are_not_detected(tmp_path):
    assert detect_enterprise(tmp_path / "nope.csv") is None
    assert detect_enterprise(None) is None


# -- anonymization -------------------------------------------------------------


def test_anonymized_labels_follow_spend_and_drop_identifiers():
    """alice $640, bob $320, carol $20, dan $20.

    carol and dan tie on spend; carol has more tokens (650k against 80k), so
    she is user-003.
    """
    report, _ = load_enterprise_csv(OFFICIAL)
    anon = report.anonymized()
    assert anon.distinct("user_email") == ("user-001", "user-002", "user-003", "user-004")
    by_label = {}
    for original, masked in zip(report.rows, anon.rows):
        by_label.setdefault(original.user_email, masked.user_email)
        assert masked.account_uuid is None
    assert by_label == {
        "alice@example.com": "user-001",
        "bob@example.com": "user-002",
        "carol@example.com": "user-003",
        "dan@example.com": "user-004",
    }
    assert "account_uuid" not in anon.present
    # Figures are untouched.
    assert anon.spend_total == report.spend_total
