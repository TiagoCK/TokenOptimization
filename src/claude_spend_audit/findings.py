"""What a check produces, and the rules a finding must satisfy to exist.

The confidence level is the most important design decision in the project,
and one rule sits above the rest:

    Never print a dollar figure next to a `candidate` without the range and
    the assumption stated inline.

That rule is enforced in ``Finding.__post_init__`` rather than left to renderer
discipline. A finding that would violate it cannot be constructed, so the
failure surfaces in a check's own tests instead of in a report someone has
already forwarded to their CFO.

The second design decision here is separating two different claims that a
single "impact" number would blur together:

* ``measured_usd`` -- arithmetic on the data. C1's cache waste is exactly this:
  the premium paid and not recovered, computable to the cent.
* ``recoverable`` -- what could actually be saved. Often unknowable from
  aggregates. C6 can measure that $40,000 was spent overnight without being
  able to say whether any of it was avoidable.

Keeping them apart is what lets C6 stay `measured` (the spend demonstrably
occurred in that window) while claiming no savings at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Sequence


class Confidence(StrEnum):
    #: Computed directly from the data; the waste is arithmetic.
    MEASURED = "measured"
    #: A pattern consistent with waste that the data cannot confirm.
    CANDIDATE = "candidate"
    #: Context, with no savings claim attached.
    OBSERVATION = "observation"


#: Report order. Checks are ordered by strength of inference, and the report
#: wants the largest impact first -- ranking within tier satisfies both without
#: ever ranking a recoverable figure against an un-sizeable exposure.
CONFIDENCE_RANK: dict[Confidence, int] = {
    Confidence.MEASURED: 0,
    Confidence.CANDIDATE: 1,
    Confidence.OBSERVATION: 2,
}


@dataclass(frozen=True, slots=True)
class Recoverable:
    """An estimate of what could actually be saved.

    For a `candidate` this must be a genuine range with the assumptions that
    produce it. For a `measured` finding a point estimate is allowed --
    ``low == high`` -- because the arithmetic supports one.
    """

    low: Decimal
    high: Decimal
    assumptions: tuple[str, ...] = ()

    @classmethod
    def exactly(cls, amount: Decimal, *assumptions: str) -> Recoverable:
        """A point estimate. Only legitimate when the arithmetic is certain."""
        return cls(low=amount, high=amount, assumptions=tuple(assumptions))

    @property
    def is_point(self) -> bool:
        return self.low == self.high

    def __str__(self) -> str:
        if self.is_point:
            return f"${self.low:,.2f}"
        return f"${self.low:,.2f} to ${self.high:,.2f}"


@dataclass(frozen=True, slots=True)
class Evidence:
    """The numbers behind a finding, so a reader can check the arithmetic.

    The tool prints figures someone may take to their CFO. Showing the
    working is what makes that survivable.
    """

    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    note: str | None = None

    def __post_init__(self) -> None:
        for row in self.rows:
            if len(row) != len(self.columns):
                raise ValueError(
                    f"evidence row {row!r} has {len(row)} cells, "
                    f"expected {len(self.columns)}"
                )


@dataclass(frozen=True, slots=True)
class Finding:
    """One result from one check."""

    check_id: str
    title: str
    confidence: Confidence
    summary: str

    #: A figure that is pure arithmetic on the data. None when the finding is
    #: qualitative.
    measured_usd: Decimal | None = None
    #: What ``measured_usd`` actually is. Required whenever it is set, so no
    #: figure ever floats in the report without saying what it measures.
    measured_basis: str | None = None

    recoverable: Recoverable | None = None
    evidence: Evidence | None = None
    remedy: str | None = None
    #: Model, workspace, tier -- whatever the finding is about.
    scope: str = ""
    #: Caveats that belong to this finding specifically, printed with it.
    caveats: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.measured_usd is not None and not self.measured_basis:
            raise ValueError(
                f"{self.check_id}: measured_usd is set but measured_basis is "
                "empty -- every figure must say what it measures"
            )

        if self.confidence is Confidence.OBSERVATION and self.recoverable is not None:
            raise ValueError(
                f"{self.check_id}: an observation carries no savings claim by "
                "definition, but a recoverable estimate was supplied"
            )

        if self.confidence is Confidence.CANDIDATE and self.recoverable is not None:
            # The rule above: never a dollar figure next to a candidate without
            # the range and the assumption stated inline.
            if self.recoverable.is_point:
                raise ValueError(
                    f"{self.check_id}: a candidate's savings must be a range, "
                    f"not the point estimate {self.recoverable}. The data "
                    "cannot support a single number here."
                )

            if not self.recoverable.assumptions:
                raise ValueError(
                    f"{self.check_id}: a candidate's savings range must state "
                    "the assumptions it rests on, inline"
                )

        if self.recoverable is not None and self.recoverable.low > self.recoverable.high:
            raise ValueError(
                f"{self.check_id}: recoverable range is inverted "
                f"({self.recoverable.low} > {self.recoverable.high})"
            )

    # -- ranking ---------------------------------------------------------

    @property
    def sort_value(self) -> Decimal:
        """The figure this finding is ranked by within its confidence tier.

        Prefers the measured figure, because it is the one that is certain.
        """
        if self.measured_usd is not None:
            return self.measured_usd
        if self.recoverable is not None:
            return self.recoverable.high
        return Decimal("0")

    @property
    def claims_savings(self) -> bool:
        return self.recoverable is not None


def rank(findings: Sequence[Finding]) -> list[Finding]:
    """Measured before candidate before observation; within each tier, a
    savings claim before a bare measurement; dollars descending inside that.

    The report puts the largest impact first, and C1 should lead it.
    Those only conflict if unlike figures share one ranking -- and tier alone
    does not keep them apart, because C1 and C6 are both `measured`. C6 can
    measure a far larger number than C1 (a month of overnight spend) without
    any of it being recoverable. Ranked on dollars alone, the report would
    open with money that was merely spent, ahead of money that was provably
    wasted. So within a tier, what could be saved ranks ahead of what was
    only spent, and the two figures are never compared with each other.
    """
    return sorted(
        findings,
        key=lambda f: (
            CONFIDENCE_RANK[f.confidence],
            not f.claims_savings,
            -f.sort_value,
            f.check_id,
        ),
    )
