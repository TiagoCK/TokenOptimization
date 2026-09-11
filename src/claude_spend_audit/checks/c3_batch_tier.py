"""C3 -- batch tier adoption. `candidate`

PROJECT_BRIEF.md §4: "Quantify standard-tier spend and present the 50% figure
as *conditional on latency tolerance* -- always a range, never a point estimate,
because you cannot tell an async pipeline from an interactive one at this
granularity."

The ceiling here is unusually clean: the Batch API is a flat 50% discount on
both input and output, and it stacks with the caching multipliers, so it is
0.5x on all five token buckets. Half of standard-tier spend is exact.

What is not clean is how much of that spend could actually move. A batch job
and a user-facing chat request look identical in aggregate data. So, as with
C2, the floor is $0 and the ceiling is arithmetic, with the assumption printed
beside the number.

**Fast mode is excluded.** The pricing page is explicit that fast mode is not
available with the Batch API, so including `speed=fast` segments would quote a
discount that cannot be taken. Priority and flex tiers are excluded for a
different reason -- they have no published rate at all, so M1 already refuses to
price them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from ..frame import Frame, Segment, TokenBuckets
from ..findings import Confidence, Evidence, Finding, Recoverable
from ..pricing import PricingTable
from ._format import model_workspace as _describe

#: The one tier that can move to batch. Priority and flex are unpriceable;
#: batch is already there.
_ELIGIBLE_TIER = "standard"

#: Published: "Fast mode is not available with the Batch API."
_INELIGIBLE_SPEED = "fast"


def run(frame: Frame, pricing: PricingTable, config) -> Iterable[Finding]:
    eligible: dict[Segment, Decimal] = {}
    excluded_fast = Decimal("0")

    for row in frame.usage:
        segment = row.segment
        if segment.service_tier != _ELIGIBLE_TIER:
            continue

        estimate = pricing.estimate_tokens(row.tokens, segment)
        if not estimate.priced_tokens:
            continue

        if segment.speed == _INELIGIBLE_SPEED:
            # Real spend, but it cannot take the discount. Counted separately
            # so the report can say why rather than silently dropping it.
            excluded_fast += estimate.usd
            continue

        eligible[segment] = eligible.get(segment, Decimal("0")) + estimate.usd

    # The discount is a rate, so it comes from the pricing file like every
    # other rate -- §3 forbids a check from containing one. Batch costs
    # `multiplier` of standard; the saving is the rest.
    multiplier = pricing.service_tier_multiplier("batch")
    discount = 1 - multiplier

    standard_spend = sum(eligible.values(), Decimal("0"))
    ceiling = standard_spend * discount
    if ceiling < config.min_impact_usd:
        return []

    share = getattr(config, "batch_eligible_share", None)
    low = ceiling * share if share is not None else Decimal("0")

    floor_clause = (
        " hence the floor of $0."
        if share is None
        else " which is why the floor is an estimate rather than a measurement."
    )
    assumptions = [
        "Applies only to work that tolerates asynchronous processing (batches "
        "return within 24 hours). Aggregate data cannot distinguish a nightly "
        "pipeline from a user-facing request," + floor_clause,
        "Assumes the whole workload could move. In practice only some routes "
        "can, and the batch queue is not instantaneous.",
    ]
    if share is not None:
        assumptions.insert(
            0,
            f"Floor assumes {share:.0%} of standard-tier spend is batch-eligible "
            "(--batch-eligible-share), which is your estimate, not a measurement.",
        )

    # Roll up for display. Pricing happened per full Segment, because tier,
    # geo and speed all change the rates -- but two segments differing only by
    # geo share a model/workspace label, and showing them as separate rows
    # reads as a duplicate.
    by_label: dict[str, Decimal] = {}
    for segment, spend in eligible.items():
        label = _describe(segment)
        by_label[label] = by_label.get(label, Decimal("0")) + spend

    rows = sorted(by_label.items(), key=lambda kv: kv[1], reverse=True)
    evidence_rows = [
        (label, f"${spend:,.2f}", f"${spend * multiplier:,.2f}")
        for label, spend in rows
    ]
    note = (
        f"Batch is a flat {discount:.0%} discount on both input and output, and it stacks "
        "with the prompt-caching multipliers, so it applies to all five token "
        "categories."
    )
    if excluded_fast:
        note += (
            f" ${excluded_fast:,.2f} of fast-mode spend is excluded: the Batch "
            "API does not accept fast mode."
        )

    return [
        Finding(
            check_id="C3",
            title=f"Standard-tier spend that could carry the {discount:.0%} batch discount",
            confidence=Confidence.CANDIDATE,
            scope=f"{len(rows)} segment(s)",
            summary=(
                f"${standard_spend:,.2f} of standard-tier spend was measured "
                f"across {len(rows)} segment(s). Any of it that tolerates "
                f"asynchronous processing would cost {discount:.0%} less on the "
                f"Batch API."
            ),
            measured_usd=ceiling,
            measured_basis=(
                f"{discount:.0%} of measured standard-tier spend -- what the batch "
                "discount would be worth if the entire workload could move"
            ),
            recoverable=Recoverable(
                low=low, high=ceiling, assumptions=tuple(assumptions)
            ),
            evidence=Evidence(
                columns=("model / workspace", "standard spend", "at batch rate"),
                rows=tuple(evidence_rows),
                note=note,
            ),
            remedy=(
                "Identify which routes are genuinely asynchronous -- scheduled "
                "jobs, backfills, evaluation runs, bulk classification -- and "
                "move those to the Batch API. Cross-check the C6 off-hours "
                "finding, if present: spend concentrated outside business hours "
                "is often exactly this kind of work."
            ),
            caveats=(
                "Batch is not available for fast mode, Priority Tier, or Claude "
                "Managed Agents sessions.",
                "Latency, not price, is the constraint. This check measures the "
                "prize, not the feasibility.",
            ),
        )
    ]


from . import Check  # noqa: E402

CHECK = Check(
    id="C3",
    name="Batch tier adoption",
    run=run,
    # Without service_tier the export cannot distinguish standard from batch,
    # and assuming everything is standard would overstate the prize. The
    # registry raises a gap naming this check instead.
    needs_dimensions=frozenset({"service_tier"}),
)
