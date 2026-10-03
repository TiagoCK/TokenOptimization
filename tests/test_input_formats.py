"""Export formats nobody has confirmed, and what the tool does with each.

No real export has been seen for most paths, so these files are invented: the
shapes a Console download, an Excel re-save or a European spreadsheet could
plausibly take. Every one of them used to produce either a traceback or a
confidently wrong figure. The rule each test holds: a format the tool cannot
read for certain is refused with a message saying what to do, or read with a
caveat saying what is missing -- never guessed.

The base month, used throughout, is fourteen days from 2026-08-01 of
claude-sonnet-5 at 2,000,000 input and 200,000 output tokens a day:

    2.0M x $2 + 0.2M x $10 = $4 + $2 = $6.00 a day, $84.00 for fourteen days
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from claude_spend_audit.checks import CHECKS, run_checks
from claude_spend_audit.cli import main
from claude_spend_audit.frame import Frame, TokenBuckets, UsageRow
from claude_spend_audit.gaps import GapCode
from claude_spend_audit.ingest import load_cost_csv, load_usage_csv
from claude_spend_audit.ingest.enterprise_csv import detect_enterprise, load_enterprise_csv
from claude_spend_audit.ingest.schema import SchemaMismatch, ValueMismatch

ROOT = Path(__file__).resolve().parents[1]
CONSOLE_COST = str(ROOT / "schemas" / "console_cost_csv.json")
COOKBOOK_COST = str(ROOT / "schemas" / "cookbook_cost_csv.json")
DAYS = [date(2026, 8, 1) + timedelta(days=i) for i in range(14)]


def write(tmp_path: Path, name: str, lines: list[str], encoding: str = "utf-8") -> str:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding=encoding)
    return str(path)


def usage(tmp_path, name="usage.csv", *, header="Date,Model,Input tokens,Output tokens",
          day=lambda d: d.isoformat(), extra=(), sep=",", encoding="utf-8"):
    rows = [sep.join([day(d), "claude-sonnet-5", "2000000", "200000"]) for d in DAYS]
    return write(tmp_path, name, [header.replace(",", sep), *rows, *extra], encoding)


def run(capsys, *argv) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def codes(frame) -> set[GapCode]:
    return {g.code for g in frame.gaps}


# -- a missing token column is not zero tokens ------------------------------------


def test_no_cache_columns_means_no_cache_finding(tmp_path, pricing):
    """With no cache-read column, every row reads zero cache reads. C2 used to
    turn that into "almost no cache reads" and a $50.40 ceiling."""
    frame, _ = load_usage_csv(usage(tmp_path))
    assert frame.token_fields == frozenset({"uncached_input", "output"})
    assert GapCode.TOKEN_COLUMNS_NOT_EXPORTED in codes(frame)

    run_ = run_checks(frame, pricing)
    assert not any(f.check_id in ("C1", "C2", "C5") for f in run_.findings)
    skipped = dict(run_.skipped)
    for label in ("Cache write amortization (C1)", "Cache adoption gap (C2)",
                  "Output token share (C5)"):
        assert "no 'cache_read' column" in skipped[label]


def test_api_frames_always_carry_every_token_field(pricing):
    """token_fields=None is the API paths' "every bucket reported"."""
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    frame = Frame(usage=(UsageRow(start=start, end=start + timedelta(days=1),
                                  tokens=TokenBuckets(uncached_input=1)),))
    for check in CHECKS:
        assert not any(g.code is GapCode.TOKEN_COLUMN_ABSENT for g in check.unmet(frame))


def test_generic_input_header_beside_cache_columns_is_flagged(tmp_path):
    path = usage(tmp_path, header="Date,Model,Input tokens,Output tokens,Cache read tokens",
                 day=lambda d: f"{d.isoformat()}")
    lines = Path(path).read_text().splitlines()
    Path(path).write_text("\n".join([lines[0]] + [l + ",1500000" for l in lines[1:]]) + "\n")
    frame, _ = load_usage_csv(path)
    gap = next(g for g in frame.gaps if g.code is GapCode.INPUT_COLUMN_AMBIGUOUS)
    assert gap.scope == "Input tokens"


# -- cents versus dollars ---------------------------------------------------------


def test_a_cost_file_both_profiles_fit_equally_is_refused(tmp_path):
    """Date, Model, Amount, Currency fits the Console profile (cents) and the
    cookbook one (dollars) with nothing unmapped: $84.00 or $8,400.00."""
    path = write(tmp_path, "cost.csv", ["Date,Model,Amount,Currency"]
                 + [f"{d},claude-sonnet-5,600.00,USD" for d in DAYS])
    with pytest.raises(SchemaMismatch, match="cents or dollars") as exc:
        load_cost_csv(path)
    assert "--cost-profile" in str(exc.value)


def test_choosing_the_profile_settles_it(tmp_path):
    path = write(tmp_path, "cost.csv", ["Date,Model,Amount,Currency"]
                 + [f"{d},claude-sonnet-5,600.00,USD" for d in DAYS])
    from claude_spend_audit.ingest.schema import Profile

    frame, _ = load_cost_csv(path, Profile.load(CONSOLE_COST))
    assert frame.billed_total_usd == Decimal("84")
    frame, _ = load_cost_csv(path, Profile.load(COOKBOOK_COST))
    assert frame.billed_total_usd == Decimal("8400")


def test_a_unit_decided_by_coverage_is_disclosed(tmp_path):
    """A Workspace column tips the choice to the Console profile, in cents.
    Still a choice another profile disputes, so the report says so."""
    path = write(tmp_path, "cost.csv", ["Date,Workspace,Model,Amount,Currency"]
                 + [f"{d},Default,claude-sonnet-5,600.00,USD" for d in DAYS])
    frame, _ = load_cost_csv(path)
    assert frame.billed_total_usd == Decimal("84")
    gap = next(g for g in frame.gaps if g.code is GapCode.COST_UNITS_INFERRED)
    assert "cents" in gap.message and "dollars" in gap.message


def test_billed_100x_the_estimate_is_critical(tmp_path, capsys):
    """$8,400.00 billed against $84.00 estimated: 100x."""
    cost = write(tmp_path, "cost.csv", ["Date,Model,Amount,Currency"]
                 + [f"{d},claude-sonnet-5,600.00,USD" for d in DAYS])
    code, out, err = run(capsys, "audit", usage(tmp_path), "--cost", cost,
                         "--cost-profile", COOKBOOK_COST, "--json")
    assert code == 1, err
    gap = next(g for g in json.loads(out)["gaps"] if g["code"] == "cost_scale_suspect")
    assert gap["scope"] == "100x"


# -- currency -----------------------------------------------------------------------


def test_non_usd_cost_csv_is_refused(tmp_path):
    path = write(tmp_path, "cost.csv", ["Date,Model,Amount,Currency"]
                 + [f"{d},claude-sonnet-5,6.00,EUR" for d in DAYS])
    from claude_spend_audit.ingest.schema import Profile

    with pytest.raises(ValueMismatch, match="'EUR'"):
        load_cost_csv(path, Profile.load(COOKBOOK_COST))


def test_non_usd_spend_report_is_refused(tmp_path):
    path = write(tmp_path, "spend.csv", ["User email,Model,Net spend,Currency",
                                         "a@x.com,claude-opus-5,120.00,GBP"])
    with pytest.raises(ValueMismatch, match="'GBP'"):
        load_enterprise_csv(path)


# -- numbers ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["10,00", "1.200,50", "6,5"])
def test_decimal_commas_are_refused(tmp_path, raw):
    path = write(tmp_path, "spend.csv", ["User email,Model,Net spend",
                                         f'a@x.com,claude-opus-5,"{raw}"'])
    with pytest.raises(ValueMismatch, match="decimal comma"):
        load_enterprise_csv(path)


def test_thousands_separators_and_dollar_signs_still_read(tmp_path):
    path = write(tmp_path, "spend.csv", ["User email,Model,Net spend",
                                         'a@x.com,claude-opus-5,"$1,200.00"',
                                         'b@x.com,claude-sonnet-5,"1,234,567.5"'])
    report, _ = load_enterprise_csv(path)
    assert report.spend_total == Decimal("1235767.50")


def test_accounting_negatives_read_as_credits(tmp_path):
    """Thirteen days at $6.00 and one credit of $2.00: $78.00 - $2.00 = $76.00."""
    lines = ["Date,Model,Amount"] + [f"{d},claude-sonnet-5,6.00" for d in DAYS[:13]]
    lines.append(f"{DAYS[13]},claude-sonnet-5,(2.00)")
    from claude_spend_audit.ingest.schema import Profile

    frame, _ = load_cost_csv(write(tmp_path, "cost.csv", lines), Profile.load(COOKBOOK_COST))
    assert frame.billed_total_usd == Decimal("76")


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-inf"])
def test_non_finite_numbers_are_refused(tmp_path, raw):
    path = usage(tmp_path, extra=[f"2026-08-20,claude-sonnet-5,{raw},1"])
    with pytest.raises(ValueMismatch, match="not a finite number"):
        load_usage_csv(path)


def test_a_bad_value_does_not_blame_the_headers(tmp_path):
    path = usage(tmp_path, extra=["2026-08-20,claude-sonnet-5,2.0M,1"])
    with pytest.raises(ValueMismatch) as exc:
        load_usage_csv(path)
    message = str(exc.value)
    assert "Row 16" in message and "'2.0M'" in message
    assert "aliases" not in message and "columns actually present" not in message


# -- dates --------------------------------------------------------------------------------


def test_slash_dates_settled_by_a_day_above_12(tmp_path):
    frame, _ = load_usage_csv(usage(tmp_path, day=lambda d: f"{d.day:02d}/{d.month:02d}/{d.year}"))
    assert frame.period_start == datetime(2026, 8, 1, tzinfo=timezone.utc)
    frame, _ = load_usage_csv(usage(tmp_path, day=lambda d: f"{d.month}/{d.day}/{d.year}"))
    assert frame.period_start == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_slash_dates_nothing_settles_are_refused(tmp_path):
    """The 3rd to the 9th of August: every date reads as either order."""
    lines = ["Date,Model,Input tokens,Output tokens"] + [
        f"{d:02d}/08/2026,claude-sonnet-5,1,1" for d in range(3, 10)
    ]
    path = write(tmp_path, "usage.csv", lines)
    with pytest.raises(ValueMismatch, match="--date-order"):
        load_usage_csv(path)
    frame, _ = load_usage_csv(path, date_order="dmy")
    assert frame.period_start == datetime(2026, 8, 3, tzinfo=timezone.utc)
    frame, _ = load_usage_csv(path, date_order="mdy")
    assert frame.period_start == datetime(2026, 3, 8, tzinfo=timezone.utc)


def test_date_order_flag_reaches_the_loader(tmp_path, capsys):
    lines = ["Date,Model,Input tokens,Output tokens"] + [
        f"{d:02d}/08/2026,claude-sonnet-5,1,1" for d in range(3, 10)
    ]
    path = write(tmp_path, "usage.csv", lines)
    code, _, err = run(capsys, "inspect", path)
    assert code == 1 and "--date-order" in err
    code, out, err = run(capsys, "inspect", path, "--date-order", "dmy", "--json")
    assert json.loads(out)["period"]["start"].startswith("2026-08-03"), err


def test_mixed_slash_orders_are_refused(tmp_path):
    lines = ["Date,Model,Input tokens,Output tokens",
             "13/08/2026,claude-sonnet-5,1,1", "08/14/2026,claude-sonnet-5,1,1"]
    with pytest.raises(ValueMismatch, match="both day-first and month-first"):
        load_usage_csv(write(tmp_path, "usage.csv", lines))


def test_a_utc_suffix_is_read(tmp_path):
    frame, _ = load_usage_csv(usage(tmp_path, day=lambda d: f"{d} 00:00:00 UTC"))
    assert frame.period_start == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_hourly_rows_without_an_end_column_last_an_hour(tmp_path):
    """24 hourly rows on 2026-08-01 end at 2026-08-02 00:00, not 08-02 23:00."""
    lines = ["Hour,Model,Input tokens,Output tokens"] + [
        f"2026-08-01 {h:02d}:00,claude-sonnet-5,100000,10000" for h in range(24)
    ]
    frame, _ = load_usage_csv(write(tmp_path, "usage.csv", lines))
    assert frame.bucket_width == "1h"
    assert frame.period_end == datetime(2026, 8, 2, tzinfo=timezone.utc)


# -- totals, subtotals and dateless rows --------------------------------------------------


def test_a_usage_totals_row_is_excluded(tmp_path):
    frame, _ = load_usage_csv(usage(tmp_path, extra=["Total,,28000000,2800000"]))
    assert frame.tokens.uncached_input == 28_000_000
    gap = next(g for g in frame.gaps if g.code is GapCode.CSV_ROWS_EXCLUDED)
    assert gap.scope == "1 row(s): are labelled as totals"


def test_a_dateless_data_row_is_reported_not_dropped(tmp_path):
    """90M Opus tokens with no date used to vanish without a word."""
    frame, _ = load_usage_csv(usage(tmp_path, extra=[",claude-opus-5,90000000,9000000"]))
    gap = next(g for g in frame.gaps if g.code is GapCode.CSV_ROWS_EXCLUDED)
    assert "have no date" in gap.scope
    assert "99,000,000 tokens" in gap.message


SPEND = "User email,Model,Product,Total prompt tokens,Total completion tokens,Total net spend USD"
SPEND_ROWS = [
    "a@x.com,claude-opus-5,Claude Code,5000000,400000,120.00",
    "b@x.com,claude-sonnet-5,Chat,1000000,100000,10.00",
    "c@x.com,claude-sonnet-5,Chat,800000,90000,8.00",
]


def test_a_spend_report_total_row_is_not_a_user(tmp_path):
    """$120 + $10 + $8 = $138. Counting the "Total" row made it $276, with
    "Total" as the largest spender."""
    report, _ = load_enterprise_csv(
        write(tmp_path, "spend.csv", [SPEND, *SPEND_ROWS, "Total,,,6800000,590000,138.00"])
    )
    assert report.spend_total == Decimal("138")
    assert "Total" not in report.distinct("user_email")


def test_a_per_user_subtotal_is_not_counted_twice(tmp_path):
    """a@x.com's subtotal row repeats their one model row: $120, not $240."""
    report, _ = load_enterprise_csv(
        write(tmp_path, "spend.csv", [SPEND, *SPEND_ROWS, "a@x.com,,,5000000,400000,120.00"])
    )
    assert report.spend_total == Decimal("138")
    gap = next(g for g in report.gaps if "but no model" in g.scope)
    assert gap.scope.startswith("1 row(s)")


def test_a_user_with_only_unknown_models_is_kept(tmp_path):
    report, _ = load_enterprise_csv(
        write(tmp_path, "spend.csv", [SPEND, SPEND_ROWS[0], "d@x.com,,,1000,100,5.00"])
    )
    assert report.spend_total == Decimal("125")


# -- routing ---------------------------------------------------------------------------------


def test_a_dated_spend_report_is_not_read_as_usage(tmp_path, capsys):
    """Date, Input tokens and Output tokens satisfy the Console usage profile,
    which would drop every email and spend figure."""
    lines = ["Date,User email,Model,Input tokens,Output tokens,Spend"] + [
        "2026-08-01,a@x.com,claude-opus-5,5000000,400000,120.00",
        "2026-08-01,b@x.com,claude-sonnet-5,1000000,100000,10.00",
    ]
    path = write(tmp_path, "spend.csv", lines)
    assert detect_enterprise(path) is not None
    code, out, err = run(capsys, "audit", path)
    assert code == 0, err
    assert "Team/Enterprise spend report" in out
    assert "$130.00" in out


def test_a_console_usage_export_stays_on_the_usage_path(tmp_path):
    assert detect_enterprise(usage(tmp_path)) is None
    assert detect_enterprise(str(ROOT / "examples" / "sample-usage.csv")) is None


# -- file encodings and delimiters ---------------------------------------------------------------


def test_utf16_with_tabs_reads(tmp_path):
    """Excel's "Unicode Text": UTF-16 with a BOM, tab-separated."""
    frame, _ = load_usage_csv(usage(tmp_path, sep="\t", encoding="utf-16"))
    assert frame.tokens.uncached_input == 28_000_000


def test_semicolon_separated_reads(tmp_path):
    frame, _ = load_usage_csv(usage(tmp_path, sep=";"))
    assert frame.tokens.output == 2_800_000


def test_a_legacy_encoding_is_a_clear_error(tmp_path, capsys):
    path = tmp_path / "usage.csv"
    path.write_bytes("Date,Model,Workspace,Input tokens,Output tokens\n"
                     "2026-08-01,claude-sonnet-5,\xc9quipe,1,1\n".encode("latin-1"))
    code, _, err = run(capsys, "audit", str(path))
    assert code == 1
    assert "not UTF-8" in err and "Traceback" not in err


def test_bracketed_headers_match(tmp_path):
    """'Cost (USD)' folds to 'cost usd', an existing alias."""
    path = write(tmp_path, "cost.csv", ["Date,Model,Cost (USD)"]
                 + [f"{d},claude-sonnet-5,600" for d in DAYS])
    frame, mapping = load_cost_csv(path)
    assert "amount" in mapping.fields
    assert frame.billed_total_usd == Decimal("84")


# -- periods -----------------------------------------------------------------------------------------


def test_cost_for_a_shorter_period_is_flagged(tmp_path, capsys):
    """Fourteen days of usage, seven of cost: $42 billed against $84."""
    cost = write(tmp_path, "cost.csv", ["Date,Workspace,Model,Amount"]
                 + [f"{d},Default,claude-sonnet-5,600" for d in DAYS[:7]])
    code, out, err = run(capsys, "audit", usage(tmp_path), "--cost", cost, "--json")
    payload = json.loads(out)
    gap = next(g for g in payload["gaps"] if g["code"] == "periods_differ")
    assert "2026-08-01 to 2026-08-15" in gap["message"]
    assert "2026-08-01 to 2026-08-08" in gap["message"]


# -- the Usage & Cost Admin API --------------------------------------------------------------------


def _cost_page(**result):
    row = {"amount": "600.00", "currency": "USD", **result}
    return {"data": [{"starting_at": "2026-08-01T00:00:00Z",
                      "ending_at": "2026-08-02T00:00:00Z", "results": [row]}],
            "has_more": False, "next_page": None}


def test_admin_cost_rows_in_another_currency_are_refused():
    from claude_spend_audit.ingest.api_json import ResponseShapeError, load_cost_json

    with pytest.raises(ResponseShapeError, match="'JPY'"):
        load_cost_json(_cost_page(currency="JPY"))


@pytest.mark.parametrize("amount", [None, "n/a", "NaN"])
def test_admin_cost_rows_with_no_usable_amount_are_refused(amount):
    from claude_spend_audit.ingest.api_json import ResponseShapeError, load_cost_json

    with pytest.raises(ResponseShapeError):
        load_cost_json(_cost_page(amount=amount))
