"""The cookbook exporter's CSV format.

Anthropic's published notebook (observability/usage_cost_api.ipynb) ships
`export_usage_to_csv` and `export_costs_to_csv`. Unlike the Console UI export,
that schema is verifiable -- the source is public -- so those profiles are
marked `verified`.

Two things about it are load-bearing and are what these tests protect:

1. `export_usage_to_csv` SUMS the two cache-creation TTLs into one column.
   That is unrecoverable, and it is the exact distinction C1 exists to measure.
2. `export_costs_to_csv` writes `amount_usd` ALREADY DIVIDED BY 100. Treating
   it as cents, which is correct for the Console profile's `amount` column,
   would understate the bill by two orders of magnitude.
"""

from decimal import Decimal

import pytest

from claude_spend_audit.checks import CheckConfig, run_checks
from claude_spend_audit.checks.c1_cache_amortization import CHECK as C1
from claude_spend_audit.gaps import GapCode
from claude_spend_audit.ingest import load_cost_csv, load_usage_csv
from claude_spend_audit.ingest.schema import Profile, SchemaMismatch, select_profile

USAGE = "tests/fixtures/csv/cookbook_usage.csv"
COST = "tests/fixtures/csv/cookbook_cost.csv"


# --------------------------------------------------------------------------
# Profile selection
# --------------------------------------------------------------------------


def test_the_right_profile_is_selected_automatically():
    frame, _ = load_usage_csv(USAGE)
    assert frame.sources[0].profile == "cookbook_usage"
    assert frame.sources[0].profile_status == "verified"


def test_console_files_still_select_the_console_profile():
    """Auto-selection must not have broken the path that already worked."""
    frame, _ = load_usage_csv("tests/fixtures/csv/usage_console.csv")
    assert frame.sources[0].profile == "console_usage"
    assert frame.sources[0].profile_status == "provisional"


def test_verified_profiles_are_tried_before_provisional_ones():
    profile = select_profile(
        ("date", "model", "uncached_input_tokens", "output_tokens"), "usage"
    )
    assert profile.status == "verified"


def test_an_unrecognised_file_names_every_profile_it_tried():
    with pytest.raises(SchemaMismatch) as excinfo:
        select_profile(("customer", "region", "revenue"), "usage")
    message = str(excinfo.value)
    assert "cookbook_usage" in message
    assert "console_usage" in message
    assert "'customer'" in message


# --------------------------------------------------------------------------
# The 100x trap
# --------------------------------------------------------------------------


def test_amount_usd_is_dollars_not_cents():
    """$25.00 + $15.00 = $40.00.

    If this profile were treated as cents, like the Console one correctly is,
    the answer would be $0.40 -- and nothing else in the report would look
    wrong.
    """
    frame, _ = load_cost_csv(COST)
    assert frame.billed_total_usd == Decimal("40.00")


def test_the_two_cost_profiles_disagree_on_units_deliberately():
    console = Profile.load("schemas/console_cost_csv.json")
    cookbook = Profile.load("schemas/cookbook_cost_csv.json")
    assert console.amount_units == "cents"
    assert cookbook.amount_units == "dollars"


def test_the_same_figure_parses_differently_under_each_profile(tmp_path):
    """The whole reason these cannot be one profile stretched over both."""
    path = tmp_path / "amount.csv"
    path.write_text("date,amount\n2026-08-01,500\n", encoding="utf-8")

    console = Profile.load("schemas/console_cost_csv.json")
    cookbook = Profile.load("schemas/cookbook_cost_csv.json")

    as_cents, _ = load_cost_csv(path, console)
    as_dollars, _ = load_cost_csv(path, cookbook)
    assert as_cents.billed_total_usd == Decimal("5")
    assert as_dollars.billed_total_usd == Decimal("500")


# --------------------------------------------------------------------------
# The summed cache TTL, and C1
# --------------------------------------------------------------------------


def test_summed_cache_writes_land_in_their_own_bucket():
    """Not folded into either real TTL bucket, which would be a fabrication.

    2 rows x 2,000,000 = 4,000,000 tokens of unknown TTL.
    """
    frame, _ = load_usage_csv(USAGE)
    assert frame.tokens.cache_write_unsplit == 4_000_000
    assert frame.tokens.cache_write_5m == 0
    assert frame.tokens.cache_write_1h == 0
    assert frame.tokens.has_unsplit_cache_writes


def test_they_still_count_toward_totals():
    """Real spend. It is the TTL that is unknown, not the tokens."""
    frame, _ = load_usage_csv(USAGE)
    assert frame.tokens.cache_write_total == 4_000_000
    #   uncached 2,000,000 + reads 200,000 + writes 4,000,000 + output 400,000
    assert frame.tokens.total == 6_600_000


def test_c1_is_blocked(pricing):
    """C1's entire value is the 4x difference between the TTLs. Given only
    their sum it would have to assume one, and would understate waste on
    1h-heavy workloads with no visible sign that it had guessed."""
    frame, _ = load_usage_csv(USAGE)
    unmet = C1.unmet(frame)
    assert len(unmet) == 1
    assert unmet[0].code is GapCode.CACHE_TTL_NOT_SPLIT
    assert "4,000,000" in unmet[0].message
    assert "cannot run" in unmet[0].message

    run = run_checks(frame, pricing, CheckConfig())
    assert not [f for f in run.findings if f.check_id == "C1"]
    assert "Cache write amortization (C1)" in {label for label, _ in run.skipped}


def test_the_other_checks_still_run(pricing):
    """Only C1 depends on the TTL split. Blocking everything would throw away
    analysis the data fully supports."""
    frame, _ = load_usage_csv(USAGE)
    run = run_checks(frame, pricing, CheckConfig())
    blocked = {label for label, _ in run.skipped}
    assert "Cache write amortization (C1)" in blocked
    for still_runs in ("Cache adoption gap (C2)", "Batch tier adoption (C3)"):
        assert still_runs not in blocked


def test_the_gap_says_the_total_is_a_floor(pricing):
    """Priced at the cheaper 5m rate, so the figure understates rather than
    varying unpredictably -- and the report says by how much it could."""
    frame, _ = load_usage_csv(USAGE)
    gap = C1.unmet(frame)[0]
    assert "floor" in gap.message
    assert "60%" in gap.message
    assert gap.invalidates_total is True


def test_unsplit_writes_are_priced_at_the_5m_rate(pricing):
    """claude-opus-5: 5m write $6.25/MTok, 1h write $10.00/MTok.

        4,000,000 unsplit x $6.25/MTok = $25.00   (the floor)
        the same tokens at 1h          = $40.00   (the ceiling)
    """
    frame, _ = load_usage_csv(USAGE)
    from claude_spend_audit.frame import Segment, TokenBuckets

    segment = Segment(model="claude-opus-5", service_tier="standard")
    estimate = pricing.estimate_tokens(
        TokenBuckets(cache_write_unsplit=4_000_000), segment
    )
    assert estimate.usd == Decimal("25.00")
    assert estimate.unpriced_tokens == 0  # counted, not dropped


def test_a_split_cookbook_export_unblocks_c1(tmp_path, pricing):
    """If someone amends the exporter to write two columns, C1 comes back.

    The profile already accepts cache_creation_5m_tokens and
    cache_creation_1h_tokens for exactly this case.
    """
    path = tmp_path / "split.csv"
    path.write_text(
        "date,model,service_tier,workspace_id,uncached_input_tokens,output_tokens,"
        "cache_creation_5m_tokens,cache_creation_1h_tokens,cache_read_tokens,"
        "web_search_requests\n"
        "2026-08-01,claude-opus-5,standard,w,0,50000,0,2000000,200000,0\n",
        encoding="utf-8",
    )
    frame, _ = load_usage_csv(path)
    assert frame.tokens.cache_write_1h == 2_000_000
    assert frame.tokens.cache_write_unsplit == 0
    assert C1.unmet(frame) == []

    #   1h writes 2,000,000 @ $10.00 = $20.00
    #   reads       200,000 @  $0.50 =  $0.10
    #   uncached  2,200,000 @  $5.00 = $11.00
    #   waste                          = $9.10
    findings = list(C1.run(frame, pricing, CheckConfig()))
    assert findings[0].measured_usd == Decimal("9.10")


def test_selection_is_by_coverage_not_first_match():
    """Several profiles can satisfy one header row -- the cookbook profile's
    required columns are a subset of the Console export's. The profile that
    recognises the MOST of the file is the one that describes it.

    Both files below parse under either profile's required columns; only
    coverage tells them apart.
    """
    console, _ = load_usage_csv("tests/fixtures/csv/usage_console.csv")
    cookbook, _ = load_usage_csv(USAGE)
    assert console.sources[0].profile == "console_usage"
    assert cookbook.sources[0].profile == "cookbook_usage"

    # And the winner leaves nothing unrecognised in its own file.
    assert console.sources[0].unmapped_columns == ()
    assert cookbook.sources[0].unmapped_columns == ()


def test_an_explicit_profile_overrides_selection(tmp_path):
    """--usage-profile must still win, so a user can force the issue."""
    console = Profile.load("schemas/console_usage_csv.json")
    frame, _ = load_usage_csv(USAGE, console)
    assert frame.sources[0].profile == "console_usage"
    # Read under the wrong profile, the summed column is simply not recognised.
    assert "cache_creation_tokens" in frame.sources[0].unmapped_columns
