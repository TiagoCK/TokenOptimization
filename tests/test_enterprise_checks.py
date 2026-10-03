"""E1-E4, the Team/Enterprise spend-report checks, with figures by hand.

tests/fixtures/csv/enterprise_spend.csv, net spend per row:

    user   product      model             prompt     completion   net
    alice  Claude Code  claude-opus-5     8,000,000  1,000,000    600.00
    alice  Chat         Claude Sonnet 5     500,000    300,000     40.00
    bob    Claude Code  Claude Sonnet 5   4,000,000    500,000    200.00
    bob    Cowork       claude-opus-5     1,000,000    600,000    120.00
    carol  Chat         claude-haiku-4-5    300,000    100,000      8.00
    carol  Chat         Claude Sonnet 5     200,000     50,000     12.00
    dan    Claude Code  claude-opus-5       400,000     50,000     16.00
    dan    Chat         claude-haiku-4-5     20,000     10,000      4.00
                                                                 -------
                                                                1,000.00

Every check is descriptive. The most important tests here are the ones that
prove a savings claim cannot get out: the Finding contract, and the runner's
refusal of anything that is not an observation.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from claude_spend_audit.enterprise import (
    DESCRIPTIVE,
    REPORT_BANNER,
    DescriptiveOnlyViolation,
    EnterpriseCheck,
    EnterpriseConfig,
    SpendReport,
    SpendRow,
    run_enterprise_checks,
)
from claude_spend_audit.findings import Confidence, Finding, Recoverable
from claude_spend_audit.gaps import GapCode
from claude_spend_audit.ingest.enterprise_csv import load_enterprise_csv
from claude_spend_audit.render import markdown, terminal

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "csv"


@pytest.fixture(scope="module")
def report() -> SpendReport:
    loaded, _ = load_enterprise_csv(FIXTURES / "enterprise_spend.csv")
    return loaded


@pytest.fixture(scope="module")
def run(report, pricing):
    return run_enterprise_checks(report, pricing)


def _finding(run, check_id) -> Finding:
    return next(f for f in run.findings if f.check_id == check_id)


# -- the descriptive contract --------------------------------------------------


def test_every_finding_is_an_observation_with_no_savings_claim(run):
    assert [f.check_id for f in run.findings] == ["E2", "E3", "E1", "E4"]
    for finding in run.findings:
        assert finding.confidence is Confidence.OBSERVATION
        assert finding.recoverable is None
        assert not finding.claims_savings
        assert DESCRIPTIVE in finding.summary
        assert finding.measured_basis.startswith("spend observed in the report's net spend")


@pytest.mark.parametrize(
    "stub",
    [
        Finding(
            check_id="EX",
            title="stub",
            confidence=Confidence.CANDIDATE,
            summary="stub",
            recoverable=Recoverable(Decimal("0"), Decimal("10"), ("an assumption",)),
        ),
        Finding(
            check_id="EX",
            title="stub",
            confidence=Confidence.MEASURED,
            summary="stub",
        ),
    ],
)
def test_the_runner_refuses_anything_but_an_observation(report, pricing, stub):
    rogue = EnterpriseCheck(id="EX", name="Rogue", run=lambda r, p, c: [stub])
    with pytest.raises(DescriptiveOnlyViolation, match="descriptive only"):
        run_enterprise_checks(report, pricing, checks=[rogue])


# -- E1 ------------------------------------------------------------------------


def test_e1_user_concentration(run):
    """Per user: alice 640, bob 320, carol 20, dan 20.

    4 users x 10% = 0.4, rounded up to 1: the top user is alice, $640.00,
    64% of $1,000.00. Median of (640, 320, 20, 20) = (320 + 20) / 2 = $170.00.
    """
    e1 = _finding(run, "E1")
    assert e1.measured_usd == Decimal("640.00")
    assert e1.scope == "top 1 of 4 users"
    assert "64% of net spend ($640.00 of $1,000.00)" in e1.summary
    assert "$170.00" in e1.summary
    assert e1.evidence.columns == ("user", "products", "requests", "spend", "share")
    assert e1.evidence.rows[0] == (
        "alice@example.com",
        "Chat, Claude Code",
        "500",
        "$640.00",
        "64%",
    )


def test_e1_rows_are_capped(report, pricing):
    run = run_enterprise_checks(report, pricing, EnterpriseConfig(max_rows=2))
    assert len(_finding(run, "E1").evidence.rows) == 2


# -- E2 ------------------------------------------------------------------------


def test_e2_product_split(run):
    """Claude Code 600 + 200 + 16 = 816 (82%), Cowork 120 (12%),
    Chat 40 + 8 + 12 + 4 = 64 (6%)."""
    e2 = _finding(run, "E2")
    assert e2.measured_usd == Decimal("816.00")
    assert e2.evidence.rows == (
        ("Claude Code", "3", "720", "$816.00", "82%"),
        ("Cowork", "1", "50", "$120.00", "12%"),
        ("Chat", "3", "370", "$64.00", "6%"),
    )


# -- E3 ------------------------------------------------------------------------


def test_e3_model_mix(run):
    """opus-5 600 + 120 + 16 = 736 frontier (73.6% -> 74%)
    sonnet-5 40 + 200 + 12 = 252 mid (25.2% -> 25%)
    haiku-4-5 8 + 4 = 12 small (1.2% -> 1%)

    "Claude Sonnet 5" is a display name; the pricing file resolves it to
    claude-sonnet-5, so it is one row.
    """
    e3 = _finding(run, "E3")
    assert e3.measured_usd == Decimal("736.00")
    assert "74% frontier ($736.00), 25% mid ($252.00), 1% small ($12.00)" in e3.summary
    assert [row[0] for row in e3.evidence.rows] == [
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    ]
    assert e3.evidence.rows[0][4] == "$5.00 / $25.00"


def test_e3_unclassified_model_is_shown_and_reported(pricing):
    report = SpendReport(
        rows=(
            SpendRow(user_email="a@x", model="claude-opus-5", net_spend_usd=Decimal("90")),
            SpendRow(user_email="b@x", model="claude-imaginary-9", net_spend_usd=Decimal("10")),
        ),
        present=frozenset({"user_email", "model", "net_spend_usd"}),
    )
    run = run_enterprise_checks(report, pricing)
    e3 = _finding(run, "E3")
    assert "10% unclassified ($10.00)" in e3.summary
    gaps = [g for g in run.gaps if g.code is GapCode.ENTERPRISE_MODEL_UNCLASSIFIED]
    assert [g.scope for g in gaps] == ["claude-imaginary-9"]
    assert not gaps[0].invalidates_total


# -- E4 ------------------------------------------------------------------------


def test_e4_output_share(run):
    """Grouped by product / model:

        Claude Code / opus-5   8.4M in, 1.05M out   11%   below 25%
        Chat / sonnet-5        700k in,  350k out   33%   shown, $52
        Claude Code / sonnet-5 4.0M in,  500k out   11%   below 25%
        Cowork / opus-5        1.0M in,  600k out   38%   shown, $120
        Chat / haiku-4-5       320k in,  110k out   26%   shown, $12

    Shown: 1,060,000 / 3,080,000 = 34.4% -> 34%; spend 120 + 52 + 12 = $184.
    Output is 5x uncached input on all three models.
    """
    e4 = _finding(run, "E4")
    assert e4.measured_usd == Decimal("184.00")
    assert "completion tokens are 34% of tokens" in e4.summary
    assert "5x" in e4.summary
    assert [row[0] for row in e4.evidence.rows] == [
        "Cowork / claude-opus-5",
        "Chat / claude-sonnet-5",
        "Chat / claude-haiku-4-5",
    ]


def test_e4_without_spend_is_qualitative(pricing):
    report = SpendReport(
        rows=(
            SpendRow(user_email="a@x", model="claude-opus-5", prompt_tokens=100, completion_tokens=100),
        ),
        present=frozenset({"user_email", "model", "prompt_tokens", "completion_tokens"}),
    )
    run = run_enterprise_checks(report, pricing)
    assert [f.check_id for f in run.findings] == ["E4"]
    e4 = run.findings[0]
    assert e4.measured_usd is None and e4.measured_basis is None
    assert "spend" not in e4.evidence.columns


# -- preconditions and fallbacks -----------------------------------------------


def test_minimal_export_skips_what_it_cannot_support(pricing):
    report, _ = load_enterprise_csv(FIXTURES / "enterprise_spend_minimal.csv")
    run = run_enterprise_checks(report, pricing)
    assert [f.check_id for f in run.findings] == ["E1", "E3"]
    assert [label for label, _ in run.skipped] == [
        "Spend by product (E2)",
        "Output token share (E4)",
    ]
    absent = {g.scope for g in run.gaps if g.code is GapCode.ENTERPRISE_FIELD_ABSENT}
    assert absent == {"product", "prompt tokens", "completion tokens"}


def test_gross_is_used_when_net_is_absent_and_says_so(pricing):
    report = SpendReport(
        rows=(
            SpendRow(user_email="a@x", model="claude-opus-5", gross_spend_usd=Decimal("75")),
            SpendRow(user_email="b@x", model="claude-sonnet-5", gross_spend_usd=Decimal("25")),
        ),
        present=frozenset({"user_email", "model", "gross_spend_usd"}),
    )
    run = run_enterprise_checks(report, pricing)
    e1 = _finding(run, "E1")
    assert e1.measured_usd == Decimal("75")
    assert "gross spend" in e1.measured_basis


def test_no_spend_column_skips_every_spend_check(pricing):
    report = SpendReport(
        rows=(SpendRow(user_email="a@x", model="claude-opus-5", prompt_tokens=10, completion_tokens=1),),
        present=frozenset({"user_email", "model", "prompt_tokens", "completion_tokens"}),
    )
    run = run_enterprise_checks(report, pricing)
    assert len(run.skipped) == 3
    assert any(g.scope == "net or gross spend" for g in run.gaps)


def test_min_impact_drops_small_findings(report, pricing):
    run = run_enterprise_checks(report, pricing, EnterpriseConfig(min_impact_usd=Decimal("10000")))
    assert run.findings == ()
    assert len(run.clean) == 4


# -- rendering -----------------------------------------------------------------


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_terminal_output_is_labelled_descriptive(report, pricing, run):
    out = terminal.render_enterprise_header(report, pricing, title="t")
    out.extend(terminal.render_findings(run.findings))
    text = "\n".join(out)
    assert REPORT_BANNER in _flat(text)
    assert text.count("not claimed") >= 2 * len(run.findings)  # table + detail
    assert "$1,000.00" in text and "$1,250.00" in text
    assert "20.0% below gross" in text
    assert "cache read tokens     not in export" in text


def test_markdown_output_is_labelled_descriptive(report, pricing, run):
    text = markdown.render_enterprise(report, pricing, run.findings, version="0.1.0")
    assert text.startswith("# Claude Team/Enterprise spend overview")
    assert REPORT_BANNER in text
    table = [line for line in text.splitlines() if line.startswith("| [E")]
    assert len(table) == 4 and all("| not claimed |" in line for line in table)
    assert "Recoverable: not claimed" in text
    assert "schema `provisional`" in text
