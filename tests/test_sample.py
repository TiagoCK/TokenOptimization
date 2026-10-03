"""The committed sample: examples/sample-usage.csv, sample-cost.csv, and the
report built from them.

The report is the first thing a new reader sees, so it is held to more than "it renders". Its preamble says every check has something to say,
and a test enforces that. It says anyone can reproduce it, and a test runs the
CLI on the committed files to prove it. And a stale sample is a public
correctness problem, so a test fails the moment regeneration would change a
byte.

    PYTHONPATH=src python -m tests.generate_sample
"""

from decimal import Decimal

import pytest

from claude_spend_audit.checks import CHECKS
from claude_spend_audit.cli import main

from ._sample import (
    MONTH,
    RUNAWAY_DAY,
    SAMPLE,
    SAMPLE_COST,
    SAMPLE_USAGE,
    build,
    build_cost_csv,
    build_usage_csv,
    run_sample,
)

REGENERATE = "regenerate with `PYTHONPATH=src python -m tests.generate_sample`"


@pytest.fixture(scope="module")
def sample():
    return run_sample()


# --------------------------------------------------------------------------
# The committed files are current
# --------------------------------------------------------------------------


def test_committed_usage_csv_is_current():
    assert SAMPLE_USAGE.read_text(encoding="utf-8") == build_usage_csv(), REGENERATE


def test_committed_cost_csv_is_current():
    assert SAMPLE_COST.read_text(encoding="utf-8") == build_cost_csv(), REGENERATE


def test_committed_report_is_current():
    """examples/sample-report.md is linked from the README."""
    assert SAMPLE.read_text(encoding="utf-8") == build(), REGENERATE


def test_generation_is_deterministic():
    """No random, no clock. If two runs differed, "is current" could not be
    tested at all."""
    assert build_usage_csv() == build_usage_csv()
    assert build_cost_csv() == build_cost_csv()


def test_the_report_carries_no_machine_specific_paths():
    text = SAMPLE.read_text(encoding="utf-8")
    assert "examples/sample-usage.csv" in text
    assert "\\" not in text
    assert "Users" not in text


# --------------------------------------------------------------------------
# It does what its preamble says
# --------------------------------------------------------------------------


def test_every_registered_check_has_something_to_say(sample):
    """The preamble claims it. The old four-row sample made the same claim
    while triggering three checks out of eight."""
    _, _, _, run = sample
    assert {f.check_id for f in run.findings} == {c.id for c in CHECKS}
    assert run.skipped == ()
    assert run.clean == ()


def test_it_says_it_is_synthetic_and_how_to_run_it():
    text = SAMPLE.read_text(encoding="utf-8")
    assert "This is a sample" in text
    assert "not from anyone's real usage data" in text
    assert "claude-spend-audit audit examples/sample-usage.csv --cost examples/sample-cost.csv" in text


def test_c1_leads_the_report(sample):
    """C1 is the strongest check in the tool, and leads the report.
    It measures less money here than C6b does -- which is exactly the case
    the ranking has to get right."""
    _, _, _, run = sample
    assert run.findings[0].check_id == "C1"
    c6b = next(f for f in run.findings if f.check_id == "C6b")
    assert c6b.measured_usd > run.findings[0].measured_usd


# --------------------------------------------------------------------------
# Anyone can reproduce it
# --------------------------------------------------------------------------


def _findings_table(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("| [C")]


def test_the_cli_reproduces_the_committed_findings(tmp_path, capsys):
    """The preamble tells a reader to run one command. It has to give them
    the report they are looking at."""
    out = tmp_path / "report.md"
    code = main([
        "audit", str(SAMPLE_USAGE), "--cost", str(SAMPLE_COST),
        "--format", "markdown", "-o", str(out),
    ])
    capsys.readouterr()
    assert code == 0  # nothing in the sample understates the total
    reproduced = _findings_table(out.read_text(encoding="utf-8"))
    committed = _findings_table(SAMPLE.read_text(encoding="utf-8"))
    assert len(committed) == len(CHECKS)
    assert reproduced == committed


def test_the_terminal_report_fits_an_email(capsys):
    """Output is hard-wrapped to 78 columns so it survives being pasted."""
    main(["audit", str(SAMPLE_USAGE), "--cost", str(SAMPLE_COST)])
    overlong = [ln for ln in capsys.readouterr().out.splitlines() if len(ln) > 78]
    assert not overlong, overlong[:3]


# --------------------------------------------------------------------------
# The story the data tells holds together
# --------------------------------------------------------------------------


def test_billed_minus_estimated_is_exactly_the_code_execution(sample):
    """Token lines in the cost file are priced from the same rate card, so
    the only difference is code execution, which usage never sees:

        21 weekdays in August 2026 x $4.20 = $88.20

    Compared to the cent, because the cost file rounds each line to a
    hundredth of a cent -- the API reports decimal strings in cents, and a
    fabricated export that carried more precision than the real one would be
    testing the wrong thing. The rounding leaves a fraction of a cent across
    600-odd lines.
    """
    frame, _, estimate, _ = sample
    weekdays = sum(1 for day in MONTH if day.weekday() < 5)
    assert weekdays == 21
    difference = frame.billed_total_usd - estimate.usd
    assert difference.quantize(Decimal("0.01")) == Decimal("88.20")


def test_the_runaway_day_is_the_only_spike(sample):
    """The month starts on a Saturday and every weekday is heavier than the
    weekend -- the shape that produced false spikes under the old 3-day
    baseline. Only the day the ETL job got stuck may be flagged."""
    _, _, _, run = sample
    c6a = next(f for f in run.findings if f.check_id == "C6a")
    assert [row[0] for row in c6a.evidence.rows] == [RUNAWAY_DAY.isoformat()]


def test_c2_does_not_contradict_c4(sample):
    """The research agent caches heavily; its playground traffic does not.
    Labelled by model / workspace alone, C2 said the research workspace read
    nothing from cache while C4 showed it reading hundreds of millions of
    tokens. With the key in the label, the row names the playground."""
    _, _, _, run = sample
    c2 = next(f for f in run.findings if f.check_id == "C2")
    labels = [row[0] for row in c2.evidence.rows]
    assert "claude-opus-5 / wrkspc_research / no API key (Console)" in labels
    assert not any("apikey_research_agent" in label for label in labels)


def test_the_estimate_has_no_pricing_caveats(sample):
    """Every price-changing dimension is present and every long-context model
    is 4.6+, so the only caveats are the ones that apply to any Console
    export."""
    frame, _, _, _ = sample
    codes = {g.code for g in frame.gaps}
    assert "pricing_dimension_not_grouped" not in codes
    assert "long_context_rate_unconfirmed" not in codes
    assert not any(g.invalidates_total for g in frame.gaps)
