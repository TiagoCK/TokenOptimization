"""`describe`: a file's format, with none of its contents.

This is what a company is asked to send back when an export is refused or
mis-read, so the property that matters is absolute: no value from the file
appears in the output. The only exception is fields whose values are API
enumerations or model IDs (currency, service tier, model, product...), which
identify no one and are often exactly what is mismatched.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import pytest

from claude_spend_audit.cli import main
from claude_spend_audit.describe import describe, shape_of

from . import _analytics as A

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

#: Columns and JSON keys whose values describe may print.
ENUMERATED = {
    "model", "product", "currency", "service_tier", "context_window",
    "inference_geo", "speed", "cost_type", "token_type", "model_family",
}


def write(tmp_path: Path, name: str, lines: list[str]) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def enumerated_headers(output: str) -> set[str]:
    """Headers describe mapped to an enumerated field, read off its output."""
    found = set()
    for line in output.splitlines():
        match = re.match(r"^\s+'(.+?)'\s+-> (\w+)$", line)
        if match and match.group(2) in ENUMERATED:
            found.add(match.group(1).strip().lower().replace(" ", "_"))
    return found


def csv_values(path: Path, allowed: set[str] = frozenset()) -> set[str]:
    """Every cell worth worrying about: not an enumerated column, long
    enough that finding it in the output cannot be a coincidence."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    headers = [h.strip().lower().replace(" ", "_") for h in rows[0]]
    header_text = " ".join(rows[0]).lower()
    out = set()
    for row in rows[1:]:
        for header, cell in zip(headers, row):
            if header in ENUMERATED or header in allowed:
                continue
            # A cell that also occurs in a header ("Total" beside "Total
            # spend") would be found in the output for the wrong reason.
            if len(cell.strip()) >= 4 and cell.strip().lower() not in header_text:
                out.add(cell.strip())
    return out


def json_values(value, key: str = "") -> set[str]:
    skip = ENUMERATED | {"endpoint", "group_by", "version", "kind", "bucket_width",
                         "source", "type"}
    out: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            if k not in skip:
                out |= json_values(v, k)
    elif isinstance(value, list):
        for item in value:
            out |= json_values(item, key)
    elif value is not None and not isinstance(value, bool) and len(str(value)) >= 4:
        out.add(str(value))
    return out


def assert_no_values(output: str, values: set[str]) -> None:
    leaked = sorted(v for v in values if v in output)
    assert not leaked, f"describe printed data values: {leaked}"


SPEND = [
    "User email,Model,Product,Total prompt tokens,Total completion tokens,Total net spend USD",
    "alice.smith@example.com,claude-opus-5,Claude Code,5000000,400000,120.00",
    "bob.jones@example.com,claude-sonnet-5,Chat,1000000,100000,10.00",
    "Total,,,6000000,500000,130.00",
]


@pytest.mark.parametrize(
    "path",
    [
        ROOT / "examples" / "sample-usage.csv",
        ROOT / "examples" / "sample-cost.csv",
        FIXTURES / "csv" / "enterprise_spend.csv",
        FIXTURES / "csv" / "enterprise_spend_variant.csv",
    ],
    ids=lambda p: p.name,
)
def test_shipped_csvs_leak_nothing(path):
    out = describe(path)
    assert_no_values(out, csv_values(path, enumerated_headers(out)))


def test_a_spend_report_leaks_no_email_or_figure(tmp_path):
    path = write(tmp_path, "spend.csv", SPEND)
    out = describe(path)
    assert_no_values(out, csv_values(path))
    assert "email-like x2, totals label x1" in out
    assert "would be read as: Team/Enterprise spend report" in out
    assert "loads 2 row(s)" in out


def test_a_refusal_shows_the_shape_not_the_value(tmp_path):
    path = write(tmp_path, "usage.csv", [
        "Date,Model,Workspace,Input tokens,Output tokens",
        "2026-08-01,claude-sonnet-5,Secret Project,12.5M,400000",
    ])
    out = describe(path)
    assert "12.5M" not in out and "Secret Project" not in out
    assert "<abbreviated number (K/M/B)>" in out
    assert "refused:" in out


def test_an_ambiguous_cost_file_is_explained(tmp_path):
    path = write(tmp_path, "cost.csv", ["Date,Model,Amount,Currency",
                                        "2026-08-01,claude-sonnet-5,600.00,USD"])
    out = describe(path)
    assert "cents or dollars" in out
    assert "600.00" not in out
    assert "would be read as: nothing" in out
    assert str(Path.home()) not in out


def test_an_unreadable_encoding_is_reported(tmp_path):
    path = tmp_path / "usage.csv"
    path.write_bytes("Date,Workspace\n2026-08-01,\xc9quipe secr\xe8te\n".encode("latin-1"))
    out = describe(path)
    assert "not UTF-8" in out
    assert "quipe" not in out


def test_an_analytics_fetch_leaks_nothing(tmp_path):
    payload = A.fetched()
    path = tmp_path / "fetch.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    out = describe(path)
    assert_no_values(out, json_values(payload))
    assert "a@example.com" not in out and "Deleted User" not in out
    assert "actor.email_address: null, string (email-like)" in out
    assert "series 'groups_cost'" in out
    assert "RBAC coverage present" in out


def test_an_admin_fetch_leaks_nothing(tmp_path, capsys):
    pages = json.loads((FIXTURES / "api" / "usage_report.json").read_text())
    pages = pages.get("pages", pages) if isinstance(pages, dict) else pages
    from datetime import datetime, timezone

    from claude_spend_audit.ingest.api_client import envelope

    payload = envelope(
        endpoint="usage",
        starting_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        ending_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
        bucket_width="1d",
        group_by=["model", "workspace_id"],
        pages=pages if isinstance(pages, list) else [pages],
        version="0.1.0",
    )
    path = tmp_path / "fetch.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    out = describe(path)
    assert_no_values(out, json_values(payload))
    assert "saved fetch: Usage & Cost Admin API" in out


def test_shapes():
    assert shape_of("2026-08-01") == "date 9999-99-99"
    assert shape_of("03/08/2026") == "slash date 99/99/9999"
    assert shape_of("1,234,567") == "integer with , thousands"
    assert shape_of("1.200,50") == "decimal comma"
    assert shape_of("$1,200.00") == "$ amount"
    assert shape_of("Grand Total") == "totals label"
    assert shape_of("someone@example.com") == "email-like"
    assert shape_of("") == "empty"


def test_the_cli_command(tmp_path, capsys):
    path = write(tmp_path, "spend.csv", SPEND)
    code = main(["describe", str(path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Never paste the file itself" in out
    assert "alice.smith" not in out
