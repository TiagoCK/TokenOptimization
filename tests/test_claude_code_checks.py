"""K1-K3, with every figure from tests/_claude_code.py worked by hand."""

from __future__ import annotations

from decimal import Decimal

import pytest

from claude_spend_audit.claude_code import CodeConfig, run_code_checks
from claude_spend_audit.claude_code.checks import CodeCheck
from claude_spend_audit.enterprise import DescriptiveOnlyViolation
from claude_spend_audit.findings import Confidence, Finding, Recoverable
from claude_spend_audit.ingest.claude_code_json import load_claude_code

from . import _claude_code as C


def _run(config=None):
    return run_code_checks(load_claude_code(C.fetched()).activity, config)


def _finding(check_id, config=None):
    return next(f for f in _run(config).findings if f.check_id == check_id)


def test_every_finding_is_a_bare_observation():
    for finding in _run().findings:
        assert finding.confidence is Confidence.OBSERVATION
        assert finding.recoverable is None


def test_k1_acceptance_by_hand():
    """All: 111 of 156 accepted = 71%. Alice: 36 of 75 = 48%, flagged."""
    k1 = _finding("K1")
    assert "71% of Claude Code's edit proposals were accepted (111 of 156)" in k1.summary
    rows = {row[0]: row for row in k1.evidence.rows}
    assert rows["Edit"][1:4] == ("90", "36", "71%")
    assert rows["Write"][1:4] == ("6", "9", "40%")
    assert "NotebookEdit" not in rows  # no proposals, no row
    assert rows["alice@example.com"][1:] == ("36", "39", "48%", "$102.75")
    assert "carol@example.com" not in rows
    assert k1.measured_usd == Decimal("102.75")
    assert "not the cost of the rejected proposals" in k1.measured_basis


def test_k1_thresholds_are_respected():
    k1 = _finding("K1", CodeConfig(low_acceptance=Decimal("0.40")))
    assert "alice@example.com" not in {row[0] for row in k1.evidence.rows}
    assert k1.measured_usd is None


def test_k2_flags_the_one_outsized_day():
    """Median per-session cost 6.875; alice's day 3 is 78.25, 11.4x."""
    k2 = _finding("K2")
    assert k2.evidence.rows == (
        ("alice@example.com", "2026-08-05", "1", "$78.25", "$78.25", "11.4x"),
    )
    assert k2.measured_usd == Decimal("78.25")
    assert "$6.88" in k2.summary


def test_k2_needs_enough_days_to_have_a_median():
    assert "Cost per session (K2)" in _run(CodeConfig(min_actor_days=10)).clean


def test_k3_split_by_billing_type():
    """API $107.25 (70%), subscription $45.00 (30%)."""
    k3 = _finding("K3")
    assert k3.evidence.rows == (
        ("API (pay as you go)", "2", "14", "$107.25", "70%"),
        ("subscription (Pro/Team)", "1", "3", "$45.00", "30%"),
    )
    assert k3.measured_usd == Decimal("45.00")
    assert "not what they were billed" in k3.summary


def test_a_savings_claim_from_a_k_check_is_refused():
    def overclaim(activity, config):
        return [Finding(check_id="K9", title="t", confidence=Confidence.CANDIDATE, summary="s",
                        recoverable=Recoverable(Decimal("0"), Decimal("1"), ("a",)))]

    with pytest.raises(DescriptiveOnlyViolation):
        run_code_checks(load_claude_code(C.fetched()).activity,
                        checks=[CodeCheck(id="K9", name="bad", run=overclaim)])
