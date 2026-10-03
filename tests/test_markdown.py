"""The Markdown renderer -- the shareable artifact.

It has to stand alone: it is what gets forwarded internally after someone
runs the tool. So the tests here are mostly about
whether a reader who did not run the tool, and has no reason to trust it, can
still tell where every number came from.
"""

import re
from decimal import Decimal

import pytest

from claude_spend_audit.checks import CheckConfig, run_checks
from claude_spend_audit.frame import merge
from claude_spend_audit.ingest import load_cost_csv, load_usage_csv
from claude_spend_audit.render import markdown


@pytest.fixture(scope="module")
def report(pricing, usage_profile, cost_profile):
    usage, _ = load_usage_csv("tests/fixtures/csv/usage_console.csv", usage_profile)
    cost, _ = load_cost_csv("tests/fixtures/csv/cost_console.csv", cost_profile)
    frame = merge(usage, cost)
    estimate = pricing.estimate_frame(frame)
    frame = frame.with_gaps(estimate.gaps)
    run = run_checks(frame, pricing, CheckConfig())
    frame = frame.with_gaps(run.gaps)
    return markdown.render(
        frame, pricing, estimate, run.findings, skipped=run.skipped, version="0.1.0"
    )


# --------------------------------------------------------------------------
# It has to stand alone
# --------------------------------------------------------------------------


def test_states_the_period(report):
    assert "2026-08-01 to 2026-08-03" in report


def test_states_both_spend_figures_separately(report):
    """Billed and estimated are two different claims and are never merged."""
    assert "| Billed | $30.75 |" in report
    assert "| Estimated | $27.75 |" in report
    assert "| Difference | -$3.00 |" in report
    assert "deliberately not merged" in report


def test_states_the_pricing_file_and_its_fetch_date(report):
    """Without this the reader cannot check a single number."""
    assert "pricing/2026-09.json" in report
    assert "2026-09-09" in report  # fetched_at
    assert "platform.claude.com" in report  # source_url


def test_names_its_source_files(report):
    assert "usage_console.csv" in report
    assert "cost_console.csv" in report


def test_carries_the_caveats_section(report):
    """A caveats section listing every gap that applied."""
    assert "## What this data cannot show" in report
    assert "Priority Tier" in report
    assert "provisional" in report


def test_says_processing_was_local(report):
    assert "No data was uploaded" in report


def test_reports_unpriced_tokens(report):
    assert "6,000,000 tokens are not priced" in report


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


def test_summary_table_shows_confidence_and_both_figures(report):
    assert "| Check | Confidence | Measured | Recoverable | Finding |" in report
    assert "candidate" in report
    assert "observation" in report


def test_a_candidates_assumptions_sit_beside_its_number(report):
    """Never a dollar figure next to a candidate without the assumption
    stated inline. In Markdown that means the same section, not a footnote."""
    c3 = report.split("### C3", 1)[1].split("### ", 1)[0]
    assert "**Recoverable: $0.00 to $11.38**" in c3
    assert "tolerates asynchronous processing" in c3


def test_an_observation_says_it_claims_nothing(report):
    c7 = report.split("### C7", 1)[1].split("### ", 1)[0]
    assert "**Recoverable: not claimed.**" in c7


def test_summary_links_resolve_to_headings(report):
    """Anchors must point at headings that exist, or the report is broken for
    the person it was forwarded to."""
    linked = set(re.findall(r"\[(\w+)\]\(#(\w+)\)", report))
    for check_id, anchor in linked:
        assert f"### {check_id}" in report
        assert anchor == check_id.lower()


# --------------------------------------------------------------------------
# It has to be valid Markdown
# --------------------------------------------------------------------------


def test_tables_are_well_formed(report):
    """Every table row has the same cell count as its header, and every header
    is followed by a separator row."""
    lines = report.splitlines()
    i = 0
    tables = 0
    while i < len(lines):
        if lines[i].startswith("|"):
            header = lines[i]
            assert i + 1 < len(lines), "table header with no separator"
            separator = lines[i + 1]
            assert re.fullmatch(r"\|(?:-{3}\|)+", separator), separator
            width = header.count("|")
            j = i + 2
            while j < len(lines) and lines[j].startswith("|"):
                assert lines[j].count("|") == width, f"ragged row: {lines[j]}"
                j += 1
            tables += 1
            i = j
        else:
            i += 1
    assert tables >= 4


def test_pipes_in_values_are_escaped():
    """A workspace name containing a pipe would otherwise split a table row."""
    from claude_spend_audit.render.markdown import _table

    rows = _table(("a",), (("we|rd",),))
    assert rows[-1] == "| we\\|rd |"


def test_output_is_pure_ascii(report):
    """This file gets forwarded, opened in unknown editors, and piped through
    consoles with a non-UTF-8 codepage."""
    non_ascii = [c for c in report if ord(c) > 127]
    assert not non_ascii, f"non-ASCII characters: {set(non_ascii)}"


def test_money_is_never_a_bare_float(report):
    """Every dollar figure should be formatted to exactly two decimals."""
    for amount in re.findall(r"\$[\d,]+\.\d+", report):
        decimals = amount.split(".")[-1]
        assert len(decimals) == 2, amount
