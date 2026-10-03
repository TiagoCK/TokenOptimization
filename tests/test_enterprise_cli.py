"""`audit` and `inspect` on a Team/Enterprise spend report.

The file is recognised by its columns, so the user runs the same command as
for any other export. These tests hold the routing to that: a spend report
goes to its own pipeline, and a usage export never does.
"""

import json
from pathlib import Path

import pytest

from claude_spend_audit.cli import main

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "csv"
ROOT = Path(__file__).resolve().parents[1]
OFFICIAL = str(FIXTURES / "enterprise_spend.csv")
VARIANT = str(FIXTURES / "enterprise_spend_variant.csv")


def run(capsys, *argv) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_audit_routes_a_spend_report(capsys):
    code, out, err = run(capsys, "audit", OFFICIAL)
    assert code == 0, err
    assert "report type      Team/Enterprise spend report" in out
    assert "estimated (usage x pricing)" not in out
    for check_id in ("[E1]", "[E2]", "[E3]", "[E4]"):
        assert check_id in out
    assert "4 descriptive finding(s) from 4 of 4 checks. None claims a saving." in out


def test_audit_markdown_to_a_file(capsys, tmp_path):
    target = tmp_path / "report.md"
    code, out, err = run(capsys, "audit", OFFICIAL, "--format", "markdown", "-o", str(target))
    assert code == 0
    assert out == ""
    assert f"wrote {target}" in err
    assert target.read_text(encoding="utf-8").startswith("# Claude Team/Enterprise spend overview")


def test_audit_json_is_marked_descriptive(capsys):
    code, out, _ = run(capsys, "audit", OFFICIAL, "--json")
    payload = json.loads(out)
    assert code == 0
    assert payload["report_type"] == "enterprise_spend"
    assert payload["descriptive_only"] is True
    assert payload["spend"] == {
        "net_usd": "1000.00",
        "gross_usd": "1250.00",
        "analysed_column": "net_spend_usd",
    }
    assert payload["totals"]["cache_read_tokens"] is None
    assert [f["check_id"] for f in payload["findings"]] == ["E2", "E3", "E1", "E4"]
    for finding in payload["findings"]:
        assert finding["confidence"] == "observation"
        assert finding["recoverable"] is None
        assert isinstance(finding["measured_usd"], str)


def test_inspect_lists_what_would_run(capsys):
    code, out, _ = run(capsys, "inspect", OFFICIAL)
    assert code == 0
    assert "No checks run. 4 of 4 would run on this data" in out


def test_inspect_json_names_blocked_checks(capsys):
    _, out, _ = run(capsys, "inspect", str(FIXTURES / "enterprise_spend_minimal.csv"), "--json")
    payload = json.loads(out)
    assert payload["checks_blocked"] == ["Spend by product (E2)", "Output token share (E4)"]


def test_explain_schema_shows_the_mapping(capsys):
    code, out, _ = run(capsys, "inspect", VARIANT, "--explain-schema")
    assert code == 0
    assert "schema profile: enterprise_spend  [provisional]" in out
    assert "Input Tokens" in out and "prompt_tokens" in out
    assert "(ignored)" in out  # the Team column


@pytest.mark.parametrize(
    "extra",
    [(), ("--json",), ("--format", "markdown")],
)
def test_anonymize_leaves_no_email_anywhere(capsys, extra):
    code, out, _ = run(capsys, "audit", OFFICIAL, "--anonymize", *extra)
    assert code == 0
    assert "@" not in out
    assert "user-001" in out


def test_cost_is_rejected_for_a_spend_report(capsys):
    code, _, err = run(capsys, "audit", OFFICIAL, "--cost", str(FIXTURES / "cost_console.csv"))
    assert code == 2
    assert "already carries spend" in err


def test_ambiguous_spend_report_explains_itself(capsys, tmp_path):
    path = tmp_path / "spend.csv"
    path.write_text(
        "email,model,spend,net spend\nx@example.com,claude-opus-5,10.00,9.00\n",
        encoding="utf-8",
    )
    code, _, err = run(capsys, "audit", str(path))
    assert code == 1
    assert "Ambiguous export" in err
    assert "net_spend_usd" in err


def test_min_impact_reaches_the_enterprise_checks(capsys):
    _, out, _ = run(capsys, "audit", OFFICIAL, "--json", "--min-impact", "700")
    payload = json.loads(out)
    # Only E2 ($816) and E3 ($736) clear $700.
    assert [f["check_id"] for f in payload["findings"]] == ["E2", "E3"]


def test_explicit_enterprise_profile_routes(capsys):
    code, out, _ = run(
        capsys,
        "audit",
        OFFICIAL,
        "--usage-profile",
        str(ROOT / "schemas" / "enterprise_spend_csv.json"),
    )
    assert code == 0
    assert "Team/Enterprise spend report" in out


def test_explicit_usage_profile_keeps_the_usage_path(capsys):
    """Forcing a Console profile onto a spend report fails on that profile."""
    code, _, err = run(
        capsys,
        "audit",
        OFFICIAL,
        "--usage-profile",
        str(ROOT / "schemas" / "console_usage_csv.json"),
    )
    assert code == 1
    assert "console_usage" in err


def test_usage_exports_still_take_the_usage_path(capsys, usage_csv):
    _, out, _ = run(capsys, "audit", str(usage_csv))
    assert "estimated (usage x pricing)" in out
    assert "Team/Enterprise spend report" not in out
