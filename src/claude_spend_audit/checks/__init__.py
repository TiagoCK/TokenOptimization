"""The check registry.

PROJECT_BRIEF.md §7: "Checks are pure functions ``Frame -> list[Finding]``,
registered in a list. Adding a check must never require touching the reporter."

Two adaptations, both forced by things the brief asks for elsewhere:

* Checks take ``(Frame, PricingTable, CheckConfig)``. Pricing is injected rather
  than imported, because §3 forbids a check from containing a rate. Config is
  injected so thresholds are visible on the command line rather than buried.

* Each check declares its own preconditions. A check that cannot run on the
  data in hand explains itself through the existing gap constructors, in its
  own words, and the reporter needs no knowledge of which checks exist.

That second point is why ingest no longer names checks. Before this, the CSV
loader emitted "batch tier adoption (C3)" from inside the parser -- a layering
violation that would drift from the registry the moment a check was renamed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Callable, Iterable, Sequence

from ..frame import UNSPLIT_CACHE_WRITE, Frame
from ..findings import Finding, rank
from ..gaps import Gap
from .. import gaps as gapmod
from ..pricing import PricingTable


@dataclass(frozen=True, slots=True)
class CheckConfig:
    """Thresholds, all overridable from the command line.

    Defaults are deliberately conservative. §7: "Bias every estimate
    conservative and state assumptions in the output itself."
    """

    #: Findings below this dollar amount are dropped rather than padding the
    #: report. A ranked list of trivia reads as noise and costs credibility.
    min_impact_usd: Decimal = Decimal("1")

    # -- C6a, spikes -----------------------------------------------------
    #: A day must exceed this multiple of the trailing median to be flagged.
    spike_threshold: Decimal = Decimal("3")
    #: Days of history used for the trailing median.
    spike_window_days: int = 7
    #: Fewer prior days than this and no baseline is trustworthy. A full week,
    #: so every baseline contains both weekdays and a weekend: with three
    #: days, data starting on a Saturday compares Tuesday against a weekend
    #: median, and weekday-heavy traffic reads as a spike that never happened.
    spike_min_history_days: int = 7

    # -- C6b, off-hours --------------------------------------------------
    #: Hours offset from UTC defining the org's local day. A fixed offset
    #: rather than a named zone: named zones need the tzdata package on
    #: Windows, and this project has no dependencies. DST is not modelled --
    #: an hour of slop that cannot change a sustained-volume conclusion.
    utc_offset_hours: Decimal = Decimal("0")
    #: Local hour business hours start (inclusive) and end (exclusive).
    business_hour_start: int = 9
    business_hour_end: int = 18
    #: Weekday numbers counted as business days (0 = Monday).
    business_days: frozenset[int] = frozenset({0, 1, 2, 3, 4})
    #: Off-hours spend must be at least this share of total to be flagged.
    off_hours_min_share: Decimal = Decimal("0.25")

    # -- C2, cache adoption ----------------------------------------------
    #: A segment reading less than this share from cache is a candidate.
    cache_adoption_max_hit_rate: Decimal = Decimal("0.20")
    #: What share of uncached input is a cacheable prefix. None means the
    #: savings range floors at $0 -- the honest default, since this is a
    #: property of the prompt that usage data never sees. Set it only from
    #: your own knowledge of the workload.
    cache_prefix_share: Decimal | None = None

    # -- C3, batch tier --------------------------------------------------
    #: What share of standard-tier spend could tolerate async processing.
    #: None floors the range at $0, for the same reason.
    batch_eligible_share: Decimal | None = None

    # -- C4, long context ------------------------------------------------
    #: Long-context spend must be at least this share of total to be shown.
    long_context_min_share: Decimal = Decimal("0.10")

    # -- C5, output share ------------------------------------------------
    #: A segment is shown when output is at least this share of its tokens.
    #: Tokens rather than spend, so caching input never raises it. At 0.25,
    #: output is over 60% of spend at uncached rates on every current model.
    output_min_token_share: Decimal = Decimal("0.25")

    # -- C7, attribution -------------------------------------------------
    #: Unattributed spend must be at least this share of total to be flagged.
    attribution_min_share: Decimal = Decimal("0.10")


#: The two cache-write buckets a TTL-summing source folds into one.
_TTL_FIELDS = frozenset({"cache_write_5m", "cache_write_1h"})

CheckFn = Callable[[Frame, PricingTable, CheckConfig], Iterable[Finding]]


@dataclass(frozen=True, slots=True)
class Check:
    """A registered check plus what it needs in order to say anything."""

    id: str
    name: str
    run: CheckFn
    #: Dimensions the data must actually be grouped by. Read against
    #: ``Frame.grouped_by``, so a dimension that is merely null everywhere
    #: never passes as present.
    needs_dimensions: frozenset[str] = frozenset()
    #: Acceptable bucket widths. None means any.
    needs_bucket_width: frozenset[str] | None = None
    #: The narrowest acceptable width, named in the gap when unmet.
    prefers_bucket_width: str | None = None
    #: Token buckets whose presence makes this check unsound. Unlike the two
    #: above, this is a precondition on the data's *content* rather than its
    #: shape: a source that summed the cache TTLs still has every dimension
    #: and every bucket width, it has just destroyed the one distinction C1
    #: is about.
    blocked_by_tokens: frozenset[str] = frozenset()
    #: Token buckets the source must carry at all. Read against
    #: ``Frame.token_fields``: a missing cache-read column would otherwise
    #: read as "no cache reads" and become a finding the data never gave.
    needs_tokens: frozenset[str] = frozenset()
    #: Distinct days of data needed, given the thresholds in force. A callable
    #: because it depends on config: C6a needs its baseline plus the day being
    #: judged. Without this, too short an export ran, found nothing, and was
    #: reported as clean -- a claim it had no basis to make.
    needs_days: Callable[["CheckConfig"], int] | None = None

    @property
    def label(self) -> str:
        return f"{self.name} ({self.id})"

    def unmet(self, frame: Frame, config: "CheckConfig | None" = None) -> list[Gap]:
        """Gaps explaining why this check cannot run, empty if it can."""
        blocking: list[Gap] = []

        for dimension in sorted(self.needs_dimensions):
            if not frame.has_dimension(dimension):
                blocking.append(gapmod.dimension_not_grouped(dimension, self.label))

        for field_name in sorted(self.needs_tokens):
            if frame.has_token_field(field_name):
                continue
            if field_name in _TTL_FIELDS and frame.has_token_field(UNSPLIT_CACHE_WRITE):
                # The source summed the TTLs into one column; the
                # cache_ttl_not_split gap below already says so.
                continue
            blocking.append(gapmod.token_column_absent(field_name, self.label))

        for field_name in sorted(self.blocked_by_tokens):
            total = sum(getattr(r.tokens, field_name, 0) for r in frame.usage)
            if total > 0:
                blocking.append(gapmod.cache_ttl_not_split(total))

        if self.needs_bucket_width is not None:
            width = frame.bucket_width
            if width is None or width not in self.needs_bucket_width:
                blocking.append(
                    gapmod.granularity_too_coarse(
                        width or "unknown",
                        self.prefers_bucket_width
                        or sorted(self.needs_bucket_width)[0],
                        self.label,
                    )
                )

        if self.needs_days is not None:
            need = self.needs_days(config or CheckConfig())
            have = len({row.start.date() for row in frame.usage})
            if have < need:
                blocking.append(gapmod.insufficient_history(have, need, self.label))

        return blocking


@dataclass(frozen=True, slots=True)
class CheckRun:
    """Everything one pass over the registry produced."""

    findings: tuple[Finding, ...] = ()
    #: Gaps raised by preconditions or by the checks themselves.
    gaps: tuple[Gap, ...] = ()
    #: Checks that ran and found nothing worth reporting.
    clean: tuple[str, ...] = ()
    #: Checks that could not run, mapped to why.
    skipped: tuple[tuple[str, str], ...] = ()


def run_checks(
    frame: Frame,
    pricing: PricingTable,
    config: CheckConfig | None = None,
    *,
    checks: Sequence[Check] | None = None,
) -> CheckRun:
    """Run every registered check whose preconditions the data satisfies."""
    config = config or CheckConfig()
    registry = list(CHECKS if checks is None else checks)

    findings: list[Finding] = []
    gaps: list[Gap] = []
    clean: list[str] = []
    skipped: list[tuple[str, str]] = []

    for check in registry:
        blocking = check.unmet(frame, config)
        if blocking:
            gaps.extend(blocking)
            skipped.append((check.label, "; ".join(g.message for g in blocking)))
            continue

        produced = list(check.run(frame, pricing, config))
        if produced:
            findings.extend(produced)
        else:
            clean.append(check.label)

    return CheckRun(
        findings=tuple(rank(findings)),
        gaps=tuple(gaps),
        clean=tuple(clean),
        skipped=tuple(skipped),
    )


def with_findings(frame: Frame, run: CheckRun) -> Frame:
    """Fold a run's gaps back into the frame, for the caveats section."""
    return frame.with_gaps(run.gaps)


# Registered last, so the check modules can import the types above.
from .c1_cache_amortization import CHECK as C1  # noqa: E402
from .c2_cache_adoption import CHECK as C2  # noqa: E402
from .c3_batch_tier import CHECK as C3  # noqa: E402
from .c4_long_context import CHECK as C4  # noqa: E402
from .c5_output_share import CHECK as C5  # noqa: E402
from .c6_anomalies import SPIKES as C6A, OFF_HOURS as C6B  # noqa: E402
from .c7_attribution import CHECK as C7  # noqa: E402
from .c8_model_mix import CHECK as C8  # noqa: E402

#: §4 orders these by strength of inference. The registry order is the
#: tie-breaker only -- actual report order comes from findings.rank().
CHECKS: tuple[Check, ...] = (C1, C2, C3, C4, C5, C6A, C6B, C7, C8)

__all__ = [
    "CHECKS",
    "Check",
    "CheckConfig",
    "CheckRun",
    "run_checks",
    "with_findings",
]
