"""C6 -- off-hours volume and day-over-day spikes. `measured`

Runaway retry loops and stuck agents are the most expensive failure mode in
production, and they show up clearly in time-bucketed usage.

Registered as two checks rather than one, because they need different data.
Spike detection works on daily buckets; off-hours detection needs hourly ones.
Lumping them would mean a daily export loses both.

**Neither claims savings.** Both are `measured` -- the spend demonstrably
occurred in that window, and the excess over a trailing median is arithmetic --
but whether any of it was avoidable is precisely what aggregate data cannot
say. A 4x Tuesday is a stuck agent or a product launch, and nothing in a token
count distinguishes them. So ``measured_usd`` is populated and ``recoverable``
is left None: the finding puts a real number in front of someone who knows
which it was, and stops there.

That restraint is the check's value. An audit tool that labels a legitimate
nightly batch job as waste will be dismissed on the spot, and rightly.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Iterable

from ..frame import Frame
from ..findings import Confidence, Evidence, Finding
from ..pricing import PricingTable


def _spend_by_row(frame: Frame, pricing: PricingTable):
    """Cost each usage row. Rows that cannot be priced are skipped.

    M1 already raised a gap naming the unpriced model or tier, so silence here
    is explained elsewhere in the report rather than swallowed.
    """
    for row in frame.usage:
        estimate = pricing.estimate_tokens(row.tokens, row.segment)
        if estimate.priced_tokens:
            yield row, estimate.usd


# --------------------------------------------------------------------------
# C6a -- day-over-day spikes
# --------------------------------------------------------------------------


def run_spikes(frame: Frame, pricing: PricingTable, config) -> Iterable[Finding]:
    daily: dict[date, Decimal] = {}
    for row, usd in _spend_by_row(frame, pricing):
        day = row.start.date()
        daily[day] = daily.get(day, Decimal("0")) + usd

    if len(daily) < config.spike_min_history_days + 1:
        return []  # no baseline worth comparing against

    ordered = sorted(daily.items())
    flagged: list[tuple[date, Decimal, Decimal, Decimal]] = []

    for index, (day, spend) in enumerate(ordered):
        if index < config.spike_min_history_days:
            continue
        window = [
            amount
            for _, amount in ordered[max(0, index - config.spike_window_days) : index]
        ]
        baseline = median(window)
        if baseline <= 0:
            continue

        multiple = spend / baseline
        excess = spend - baseline
        # Both gates matter. The multiple alone would flag a $0.30 day rising
        # to $1.00; the absolute floor keeps the report about real money.
        if multiple >= config.spike_threshold and excess >= config.min_impact_usd:
            flagged.append((day, spend, baseline, multiple))

    if not flagged:
        return []

    total_excess = sum((spend - baseline for _, spend, baseline, _ in flagged), Decimal("0"))
    worst = max(flagged, key=lambda f: f[1] - f[2])

    return [
        Finding(
            check_id="C6a",
            title="Daily spend spiked well above the recent baseline",
            confidence=Confidence.MEASURED,
            scope=f"{len(flagged)} day(s)",
            summary=(
                f"{len(flagged)} day(s) cost at least "
                f"{config.spike_threshold:g}x the median of the preceding "
                f"{config.spike_window_days} days. The largest was "
                f"{worst[0].isoformat()} at ${worst[1]:,.2f} against a "
                f"${worst[2]:,.2f} baseline."
            ),
            measured_usd=total_excess,
            measured_basis=(
                "spend above the trailing median on the flagged days -- what "
                "those days cost beyond a typical one"
            ),
            # No recoverable estimate. See the module docstring.
            evidence=Evidence(
                columns=("date", "spend", "baseline", "multiple", "excess"),
                rows=tuple(
                    (
                        day.isoformat(),
                        f"${spend:,.2f}",
                        f"${baseline:,.2f}",
                        f"{multiple:.1f}x",
                        f"${spend - baseline:,.2f}",
                    )
                    for day, spend, baseline, multiple in flagged
                ),
                note=(
                    "Baseline is the median of the preceding "
                    f"{config.spike_window_days} days, which is unmoved by an "
                    "adjacent spike in a way a mean would not be."
                ),
            ),
            remedy=(
                "Check what ran on these dates. A spike that matches a known "
                "launch or backfill is fine; one that does not is worth tracing "
                "to a retry loop or an agent that failed to terminate."
            ),
            caveats=(
                "This is a measurement, not a verdict. Aggregate data cannot "
                "distinguish a runaway job from legitimate demand, so no part "
                "of this figure is claimed as recoverable.",
                "Days are bucketed in UTC, matching the API's own boundaries.",
            ),
        )
    ]


# --------------------------------------------------------------------------
# C6b -- off-hours volume
# --------------------------------------------------------------------------


def _local(moment: datetime, offset_hours: Decimal) -> datetime:
    """Shift a UTC bucket into the org's local day.

    A fixed offset rather than a named zone: zoneinfo needs the tzdata package
    on Windows, and this project ships with no dependencies. The cost is that
    DST is not modelled -- an hour of drift at the window edges, which cannot
    change whether overnight volume is sustained.

    Converted through whole minutes rather than float hours, so half-hour and
    45-minute offsets (UTC+5:30, UTC+5:45) stay exact.
    """
    return moment + timedelta(minutes=int(offset_hours * 60))


def _is_off_hours(moment: datetime, config) -> bool:
    local = _local(moment, config.utc_offset_hours)
    if local.weekday() not in config.business_days:
        return True
    return not (config.business_hour_start <= local.hour < config.business_hour_end)


def _window_label(config) -> str:
    days = "Mon-Fri" if config.business_days == frozenset({0, 1, 2, 3, 4}) else "selected weekdays"
    offset = config.utc_offset_hours
    sign = "+" if offset >= 0 else "-"
    return (
        f"{config.business_hour_start:02d}:00-{config.business_hour_end:02d}:00 "
        f"{days}, UTC{sign}{abs(offset):g}"
    )


def run_off_hours(frame: Frame, pricing: PricingTable, config) -> Iterable[Finding]:
    total = Decimal("0")
    off_hours = Decimal("0")
    overnight = Decimal("0")
    weekend = Decimal("0")

    for row, usd in _spend_by_row(frame, pricing):
        total += usd
        if not _is_off_hours(row.start, config):
            continue
        off_hours += usd
        if _local(row.start, config.utc_offset_hours).weekday() in config.business_days:
            overnight += usd
        else:
            weekend += usd

    if total <= 0:
        return []

    share = off_hours / total
    if share < config.off_hours_min_share or off_hours < config.min_impact_usd:
        return []

    return [
        Finding(
            check_id="C6b",
            title="A large share of spend falls outside business hours",
            confidence=Confidence.MEASURED,
            scope=_window_label(config),
            summary=(
                f"{share:.0%} of priced spend (${off_hours:,.2f}) occurred "
                f"outside {_window_label(config)} -- ${overnight:,.2f} overnight "
                f"on weekdays and ${weekend:,.2f} at weekends."
            ),
            measured_usd=off_hours,
            measured_basis=(
                "spend in buckets falling outside the stated business-hours "
                "window"
            ),
            evidence=Evidence(
                columns=("window", "spend", "share of total"),
                rows=(
                    (
                        f"business hours ({_window_label(config)})",
                        f"${total - off_hours:,.2f}",
                        f"{(total - off_hours) / total:.0%}",
                    ),
                    ("overnight, weekdays", f"${overnight:,.2f}", f"{overnight / total:.0%}"),
                    ("weekends", f"${weekend:,.2f}", f"{weekend / total:.0%}"),
                ),
            ),
            remedy=(
                "Identify what runs in this window. Genuinely async work here is "
                "a candidate for the Batch API's 50% discount rather than a "
                "problem; unattended interactive traffic usually is a problem."
            ),
            caveats=(
                f"Business hours were assumed to be {_window_label(config)}. "
                "Set --utc-offset and --business-hours to match the team that "
                "owns this traffic; the figure moves with that assumption.",
                "Daylight saving is not modelled, so window edges can be off by "
                "an hour.",
                "Many workloads are legitimately scheduled overnight. This "
                "measures when the money was spent and claims nothing about "
                "whether it was wasted.",
            ),
        )
    ]


from . import Check  # noqa: E402

SPIKES = Check(
    id="C6a",
    name="Day-over-day spend spikes",
    run=run_spikes,
    # Works on daily or hourly data: hourly buckets are aggregated up.
    needs_bucket_width=frozenset({"1d", "1h"}),
    prefers_bucket_width="1d",
    # A baseline of spike_min_history_days, plus at least one day to judge.
    needs_days=lambda config: config.spike_min_history_days + 1,
)

OFF_HOURS = Check(
    id="C6b",
    name="Off-hours and weekend volume",
    run=run_off_hours,
    # Daily buckets cannot distinguish 3am from 3pm.
    needs_bucket_width=frozenset({"1h"}),
    prefers_bucket_width="1h",
)
