"""The inspect command: the §5 header block, and honest exit codes."""

import json
from decimal import Decimal

import pytest

from claude_spend_audit.cli import main


FIXTURE_USAGE = "tests/fixtures/csv/usage_console.csv"


def run(capsys, *argv) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_header_block_has_the_section_five_fields(capsys, usage_csv):
    code, out, _ = run(capsys, "inspect", str(usage_csv))
    assert "period covered" in out
    assert "2026-08-01" in out and "2026-08-03" in out
    assert "pricing file     2026-09" in out
    assert "TOKENS" in out
    assert "SPEND" in out
    assert "WHAT THIS DATA CANNOT SHOW" in out
    # §1: say it plainly, in the output itself and not only the README.
    assert "Nothing was uploaded" in out
    # Priority tier is present in the fixture, so the total is caveated.
    assert code == 1


def test_all_five_token_buckets_are_shown_separately(capsys, usage_csv):
    _, out, _ = run(capsys, "inspect", str(usage_csv))
    assert "cache write (5m)" in out
    assert "cache write (1h)" in out
    assert "cache read" in out
    assert "uncached input" in out
    assert "output" in out


def test_billed_and_estimated_are_never_merged(capsys, usage_csv, cost_csv):
    """Two different claims about the money, shown as two lines.

    Estimated: 14.50 + 4.50 + 8.25 + 0.50 web search = $27.75
    Billed:                                            $30.75
    Difference:                                        -$3.00

    The $3.00 is code execution, which is billed but never appears in usage --
    so the gap between the two lines is exactly the thing the usage endpoint
    cannot see. Merging them into one total would hide that.
    """
    _, out, _ = run(capsys, "inspect", str(usage_csv), "--cost", str(cost_csv))
    assert "billed (from cost data)" in out
    assert "estimated (usage x pricing)" in out
    assert "$27.75" in out
    assert "$30.75" in out
    assert "-$3.00" in out


def test_unpriced_tokens_are_called_out_not_folded_into_the_total(capsys, usage_csv):
    _, out, _ = run(capsys, "inspect", str(usage_csv))
    assert "NOT PRICED" in out
    assert "6,000,000 tokens" in out
    assert "rather than counted as zero" in out


def test_priority_tier_warning_is_loud(capsys, usage_csv):
    _, out, _ = run(capsys, "inspect", str(usage_csv))
    assert "!!" in out  # critical marker
    # Collapse wrapping before matching: the requirement is that the warning is
    # present and marked critical, not that it fits on one line.
    flat = " ".join(out.split())
    assert "Priority Tier uses a different billing model" in flat
    assert "understates the real bill" in flat


def test_exit_code_one_when_a_gap_invalidates_the_total(capsys, usage_csv):
    """So a wrapping script cannot mistake a caveated number for a clean one."""
    code, _, _ = run(capsys, "inspect", str(usage_csv))
    assert code == 1


def test_exit_code_zero_on_clean_data(capsys, tmp_path):
    clean = tmp_path / "clean.csv"
    clean.write_text(
        "start,model,workspace_id,service_tier,uncached_input_tokens,output_tokens\n"
        "2026-08-01T00:00:00Z,claude-opus-5,wrkspc_a,standard,1000000,200000\n"
        "2026-08-01T01:00:00Z,claude-opus-5,wrkspc_a,standard,1000000,200000\n",
        encoding="utf-8",
    )
    code, out, _ = run(capsys, "inspect", str(clean))
    assert code == 0
    #   uncached 2,000,000 @ $5.00/MTok = $10.00
    #   output     400,000 @ $25.00/MTok = $10.00
    #                                     -------
    #                                      $20.00
    assert "$20.00" in out


def test_explain_schema_shows_the_mapping(capsys, usage_csv):
    _, out, _ = run(capsys, "inspect", str(usage_csv), "--explain-schema")
    assert "schema profile: console_usage" in out
    assert "[provisional]" in out
    assert "does not publish these headers" in out
    assert "-> " in out
    assert "cache_write_1h" in out


def test_explain_schema_marks_ignored_columns(capsys, tmp_path, usage_csv):
    extra = tmp_path / "extra.csv"
    extra.write_text(
        "start,uncached_input_tokens,output_tokens,internal_note\n"
        "2026-08-01T00:00:00Z,1000,100,ignore me\n",
        encoding="utf-8",
    )
    _, out, _ = run(capsys, "inspect", str(extra), "--explain-schema")
    assert "(ignored)" in out
    assert "internal_note" in out
    # And it is surfaced as a caveat, not silently discarded.
    assert "were not recognised" in out


def test_json_output_is_machine_readable(capsys, usage_csv, cost_csv):
    _, out, _ = run(capsys, "inspect", str(usage_csv), "--cost", str(cost_csv), "--json")
    payload = json.loads(out)

    assert payload["period"]["bucket_width"] == "1d"
    assert payload["pricing"]["version"] == "2026-09"
    assert payload["pricing"]["fetched_at"] == "2026-09-09"
    assert Decimal(payload["spend"]["billed_usd"]) == Decimal("30.75")
    assert Decimal(payload["spend"]["estimated_usd"]) == Decimal("27.75")
    assert payload["spend"]["estimate_is_complete"] is False
    assert payload["spend"]["unpriced_tokens"] == 6_000_000
    assert payload["tokens"]["total"] == 13_300_000
    assert payload["tokens"]["cache_write_5m"] == 400_000
    assert payload["tokens"]["cache_write_1h"] == 100_000
    assert payload["web_search_requests"] == 50

    codes = {g["code"] for g in payload["gaps"]}
    assert "priority_tier_present" in codes
    assert "provisional_csv_schema" in codes
    # Critical gaps sort first.
    assert payload["gaps"][0]["severity"] == "critical"


def test_no_cost_file_says_the_figures_are_estimates(capsys, usage_csv):
    _, out, _ = run(capsys, "inspect", str(usage_csv))
    assert "not supplied" in out
    assert "every dollar figure is estimated" in out


def test_a_bad_csv_exits_with_a_useful_message(capsys, tmp_path):
    wrong = tmp_path / "wrong.csv"
    wrong.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    code, _, err = run(capsys, "inspect", str(wrong))
    assert code == 1
    assert "'a'" in err
    assert "aliases" in err


def test_missing_file_exits_two(capsys, tmp_path):
    code, _, err = run(capsys, "inspect", str(tmp_path / "nope.csv"))
    assert code == 2
    assert "no such file" in err


def test_missing_pricing_file_exits_two(capsys, usage_csv, tmp_path):
    code, _, err = run(
        capsys, "inspect", str(usage_csv), "--pricing", str(tmp_path / "nope.json")
    )
    assert code == 2
    assert "pricing file not found" in err


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


# --------------------------------------------------------------------------
# audit (M2)
# --------------------------------------------------------------------------

WASTE = "tests/fixtures/csv/usage_cache_waste.csv"
HOURLY = "tests/fixtures/csv/usage_hourly.csv"


def test_audit_leads_with_c1(capsys):
    """§4: 'Lead the report with it.' C1 is the only measured-waste check, so
    tier ranking puts it first without a special case."""
    _, out, _ = run(capsys, "audit", WASTE)
    assert "FINDINGS" in out
    findings_block = out.split("FINDINGS", 1)[1]
    assert findings_block.index("C1") < findings_block.index("caveats")
    assert "$9.10" in out


def test_audit_shows_confidence_on_every_row(capsys):
    """§5: 'confidence level visible on every row'."""
    import re

    _, out, _ = run(capsys, "audit", HOURLY)
    # Ranked-table rows look like "  1   C6b    measured   $74.88  not claimed"
    rows = [
        ln for ln in out.splitlines() if re.match(r"^\s+\d+\s+C\w+\s+\w+\s", ln)
    ]
    assert len(rows) >= 2, rows
    # The requirement is that EVERY row carries one, not that there are N rows.
    for row in rows:
        assert any(c in row for c in ("measured", "candidate", "observation")), row


def test_c6_says_it_claims_no_savings(capsys):
    """The distinction the whole Finding shape exists to preserve."""
    _, out, _ = run(capsys, "audit", HOURLY)
    assert "not claimed" in out
    assert "measures what was spent" in out


def test_audit_reports_blocked_checks(capsys):
    """Silence about a check that could not run would be misleading."""
    _, out, _ = run(capsys, "audit", str(FIXTURE_USAGE))
    assert "CHECKS THAT COULD NOT RUN" in out
    assert "Off-hours and weekend volume (C6b)" in out


def test_audit_findings_do_not_change_the_exit_code(capsys):
    """Finding something is the tool working, not an error. The waste fixture
    has no total-invalidating gap, so it exits 0 despite a $9.10 finding."""
    code, out, _ = run(capsys, "audit", WASTE)
    assert "$9.10" in out
    assert code == 0


def test_audit_still_flags_an_invalidated_total(capsys):
    """The M1 contract survives: priority tier in the data means exit 1."""
    code, _, _ = run(capsys, "audit", str(FIXTURE_USAGE))
    assert code == 1


def test_min_impact_filters_findings(capsys):
    _, out, _ = run(capsys, "audit", WASTE, "--min-impact", "100")
    assert "No findings" in out


def test_utc_offset_moves_the_business_window(capsys):
    at_utc = run(capsys, "audit", HOURLY, "--json")[1]
    shifted = run(capsys, "audit", HOURLY, "--utc-offset", "-5", "--json")[1]
    import json as _json

    a = next(f for f in _json.loads(at_utc)["findings"] if f["check_id"] == "C6b")
    b = next(f for f in _json.loads(shifted)["findings"] if f["check_id"] == "C6b")
    assert a["measured_usd"] != b["measured_usd"]
    assert "UTC-5" in b["scope"]


def test_business_hours_flag_is_validated(capsys):
    with pytest.raises(SystemExit):
        main(["audit", HOURLY, "--business-hours", "not-a-window"])


def test_audit_json_shape(capsys):
    import json as _json

    _, out, _ = run(capsys, "audit", WASTE, "--json")
    payload = _json.loads(out)
    finding = next(f for f in payload["findings"] if f["check_id"] == "C1")

    # Money as strings, as everywhere else.
    assert finding["measured_usd"] == "9.10"
    assert finding["recoverable"]["low"] == "9.10"
    assert finding["recoverable"]["assumptions"]
    assert finding["measured_basis"]
    assert finding["evidence"]["columns"][0] == "model / workspace / key"
    assert payload["checks_blocked"] == ["Off-hours and weekend volume (C6b)"]


def test_inspect_reports_which_checks_the_export_supports(capsys):
    """inspect gained this from the registry -- it used to be two hardcoded
    strings in the CSV parser."""
    _, out, _ = run(capsys, "inspect", str(FIXTURE_USAGE))
    assert "CHECKS THAT COULD NOT RUN" in out
    assert "use `audit`" in out


def test_output_stays_within_terminal_width(capsys):
    """Reports get pasted into email and tickets; wrapped tables are unreadable."""
    for argv in (("audit", WASTE), ("audit", HOURLY), ("inspect", str(FIXTURE_USAGE))):
        _, out, _ = run(capsys, *argv)
        overlong = [ln for ln in out.splitlines() if len(ln) > 78]
        assert not overlong, f"{argv}: {overlong[:2]}"
