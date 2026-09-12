"""The Finding contract, which is PROJECT_BRIEF.md §3 made structural.

§3 calls the confidence level "the most important design decision in the
project" and sets one rule above the rest:

    Never print a dollar figure next to a `candidate` without the range and
    the assumption stated inline.

These tests exist to prove a violating finding cannot be built. If they ever
pass while the rule is unenforced, the tool can ship an overclaim -- and §3's
whole point is that "an audit tool that overclaims dies on first contact with
a competent engineer".
"""

from decimal import Decimal

import pytest

from claude_spend_audit.findings import (
    CONFIDENCE_RANK,
    Confidence,
    Evidence,
    Finding,
    Recoverable,
    rank,
)


def make(**overrides) -> Finding:
    base = dict(
        check_id="CX",
        title="t",
        confidence=Confidence.MEASURED,
        summary="s",
    )
    return Finding(**(base | overrides))


# --------------------------------------------------------------------------
# §3's rule
# --------------------------------------------------------------------------


def test_candidate_cannot_carry_a_point_estimate():
    with pytest.raises(ValueError, match="must be a range"):
        make(
            confidence=Confidence.CANDIDATE,
            recoverable=Recoverable.exactly(Decimal("100"), "an assumption"),
        )


def test_candidate_range_must_state_its_assumptions():
    with pytest.raises(ValueError, match="assumptions"):
        make(
            confidence=Confidence.CANDIDATE,
            recoverable=Recoverable(low=Decimal("50"), high=Decimal("100")),
        )


def test_a_properly_formed_candidate_is_accepted():
    finding = make(
        confidence=Confidence.CANDIDATE,
        recoverable=Recoverable(
            low=Decimal("50"),
            high=Decimal("100"),
            assumptions=("only if the workload tolerates async latency",),
        ),
    )
    assert not finding.recoverable.is_point
    assert finding.recoverable.assumptions


def test_observation_cannot_claim_savings():
    """An observation is context with no savings claim, by §3's definition."""
    with pytest.raises(ValueError, match="no savings claim"):
        make(
            confidence=Confidence.OBSERVATION,
            recoverable=Recoverable.exactly(Decimal("10")),
        )


def test_measured_may_carry_a_point_estimate():
    """C1's whole shape: disabling caching recovers exactly the waste."""
    finding = make(
        confidence=Confidence.MEASURED,
        recoverable=Recoverable.exactly(Decimal("9.10"), "recovered by disabling"),
    )
    assert finding.recoverable.is_point


def test_an_inverted_range_is_rejected():
    with pytest.raises(ValueError, match="inverted"):
        make(
            confidence=Confidence.MEASURED,
            recoverable=Recoverable(low=Decimal("100"), high=Decimal("50")),
        )


# --------------------------------------------------------------------------
# Every figure says what it is
# --------------------------------------------------------------------------


def test_a_dollar_figure_must_say_what_it_measures():
    with pytest.raises(ValueError, match="measured_basis"):
        make(measured_usd=Decimal("500"))


def test_basis_is_not_required_without_a_figure():
    assert make(measured_usd=None).measured_basis is None


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


def test_evidence_rows_must_match_the_columns():
    with pytest.raises(ValueError, match="expected 2"):
        Evidence(columns=("a", "b"), rows=(("1", "2", "3"),))


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------


def test_confidence_outranks_dollars():
    """§4 says C1 leads the report; §5 says largest impact first.

    Ranking within tier honours both. A $40,000 exposure never displaces a
    $9 measured waste, because they are not the same kind of claim.
    """
    big_candidate = make(
        check_id="CBIG",
        confidence=Confidence.CANDIDATE,
        measured_usd=Decimal("40000"),
        measured_basis="standard-tier spend",
        recoverable=Recoverable(
            low=Decimal("1"), high=Decimal("20000"), assumptions=("async only",)
        ),
    )
    small_measured = make(
        check_id="C1",
        confidence=Confidence.MEASURED,
        measured_usd=Decimal("9.10"),
        measured_basis="cache premium not recovered",
    )
    ordered = rank([big_candidate, small_measured])
    assert [f.check_id for f in ordered] == ["C1", "CBIG"]


def test_recoverable_waste_outranks_a_larger_exposure_in_the_same_tier():
    """C1 and C6 are both `measured`. C6b can measure $12,000 of overnight
    spend, none of it claimed as recoverable; C1 measures $1,600 that
    provably was. Sorting on dollars within the tier put C6b first -- the
    report opened with money merely spent, ahead of money wasted.

    The sample month exposed this; the old four-row sample never ran C6.
    """
    exposure = make(
        check_id="C6b",
        measured_usd=Decimal("12560"),
        measured_basis="spend outside business hours",
    )
    waste = make(
        check_id="C1",
        measured_usd=Decimal("1640"),
        measured_basis="cache premium not recovered",
        recoverable=Recoverable.exactly(Decimal("1640")),
    )
    assert [f.check_id for f in rank([exposure, waste])] == ["C1", "C6b"]


def test_exposures_still_order_by_dollars_among_themselves():
    c6a = make(check_id="C6a", measured_usd=Decimal("4396"), measured_basis="b")
    c6b = make(check_id="C6b", measured_usd=Decimal("12560"), measured_basis="b")
    assert [f.check_id for f in rank([c6a, c6b])] == ["C6b", "C6a"]


def test_the_savings_split_never_crosses_tiers():
    """A candidate's claimed range still ranks below any measured finding,
    including one that claims nothing."""
    exposure = make(check_id="C6b", measured_usd=Decimal("5"), measured_basis="b")
    candidate = make(
        check_id="C3",
        confidence=Confidence.CANDIDATE,
        measured_usd=Decimal("20000"),
        measured_basis="b",
        recoverable=Recoverable(low=Decimal("0"), high=Decimal("20000"), assumptions=("a",)),
    )
    assert [f.check_id for f in rank([candidate, exposure])] == ["C6b", "C3"]


def test_dollars_order_within_a_tier():
    small = make(check_id="A", measured_usd=Decimal("10"), measured_basis="b")
    large = make(check_id="B", measured_usd=Decimal("999"), measured_basis="b")
    assert [f.check_id for f in rank([small, large])] == ["B", "A"]


def test_full_tier_order():
    findings = [
        make(check_id="OBS", confidence=Confidence.OBSERVATION),
        make(check_id="CAND", confidence=Confidence.CANDIDATE),
        make(check_id="MEAS", confidence=Confidence.MEASURED),
    ]
    assert [f.check_id for f in rank(findings)] == ["MEAS", "CAND", "OBS"]
    assert CONFIDENCE_RANK[Confidence.MEASURED] < CONFIDENCE_RANK[Confidence.OBSERVATION]


def test_ranking_is_stable_for_equal_findings():
    """Ties break on check_id, so report order does not shuffle between runs."""
    a = make(check_id="A", measured_usd=Decimal("5"), measured_basis="b")
    b = make(check_id="B", measured_usd=Decimal("5"), measured_basis="b")
    assert [f.check_id for f in rank([b, a])] == ["A", "B"]


def test_sort_value_prefers_the_measured_figure():
    finding = make(
        measured_usd=Decimal("100"),
        measured_basis="b",
        recoverable=Recoverable.exactly(Decimal("5")),
    )
    assert finding.sort_value == Decimal("100")


def test_sort_value_falls_back_to_the_recoverable_high():
    finding = make(
        confidence=Confidence.CANDIDATE,
        recoverable=Recoverable(
            low=Decimal("1"), high=Decimal("70"), assumptions=("a",)
        ),
    )
    assert finding.sort_value == Decimal("70")


def test_sort_value_is_zero_when_nothing_is_quantified():
    assert make(confidence=Confidence.OBSERVATION).sort_value == Decimal("0")
