"""C8 -- model mix. `observation`

PROJECT_BRIEF.md §4: "Frontier vs. mid vs. small model spend share. **Resist
the temptation to claim routing savings here.** You cannot tell from
aggregates whether an Opus call needed Opus. ... Overclaiming on this check
is the fastest way to lose a technical buyer."

So this check reports the mix and the list prices behind it, and stops. The
list prices are what let a reader see the gap between tiers for themselves --
the same move C4 makes with the value of a removed token: put the measured
rate in front of the person who knows the workload, and let them do the
multiplication the data cannot.

**The tiers are a classification, not a fact.** Anthropic publishes rates,
not tiers. The ``tier`` on each model lives in the pricing file, marked there
as this tool's judgement, so a reader who disagrees can edit it and re-run --
the same contract as every rate. A model with no tier is shown as
``unclassified`` rather than dropped, because dropping it would make the
shares add up to less than the spend.

Rows are keyed by canonical model id, so a display name and an API id for the
same model are one row even where alias merging had nothing to merge.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from ..frame import Frame
from ..findings import Confidence, Evidence, Finding
from ..pricing import PricingTable, Priced

#: Display order. Anything else a user writes into the file sorts after these.
_TIER_ORDER = ("frontier", "mid", "small")
_UNCLASSIFIED = "unclassified"

#: RouteLLM's abstract, quoted rather than paraphrased so the claim is exactly
#: the size its authors made it.
_ROUTING_RESEARCH = (
    "Published routing research reports large savings at high quality "
    "retention: RouteLLM (Ong et al., 2024, arXiv:2406.18665) cut costs \"by "
    "over 2 times in certain cases\" on widely-recognized benchmarks. That was "
    "on benchmark prompts, with other vendors' models and a trained router. "
    "None of it transfers to this workload without per-request evaluation."
)


def _list_price(pricing: PricingTable, model: str) -> str:
    """Standard-tier input / output per MTok, straight from the rate card."""
    base = pricing.rate_for(model, "uncached_input")
    output = pricing.rate_for(model, "output")
    if not isinstance(base, Priced) or not isinstance(output, Priced):
        return "-"
    return f"${base.usd_per_mtok:,.2f} / ${output.usd_per_mtok:,.2f}"


def _tier_rank(tier: str) -> tuple[int, str]:
    if tier in _TIER_ORDER:
        return (_TIER_ORDER.index(tier), tier)
    return (len(_TIER_ORDER), tier)


def run(frame: Frame, pricing: PricingTable, config) -> Iterable[Finding]:
    spend: dict[str, Decimal] = {}
    for row in frame.usage:
        estimate = pricing.estimate_tokens(row.tokens, row.segment)
        if not estimate.priced_tokens:
            continue  # unpriced; the estimate already raised a gap for it
        model = pricing.resolve_model(row.segment.model) or str(row.segment.model)
        spend[model] = spend.get(model, Decimal("0")) + estimate.usd

    total = sum(spend.values(), Decimal("0"))
    if total <= 0:
        return []

    tier_of = {model: pricing.tier_of(model) or _UNCLASSIFIED for model in spend}
    by_tier: dict[str, Decimal] = {}
    for model, usd in spend.items():
        by_tier[tier_of[model]] = by_tier.get(tier_of[model], Decimal("0")) + usd

    frontier = by_tier.get("frontier", Decimal("0"))
    if frontier < config.min_impact_usd:
        return []  # nothing on the most expensive tier worth a line
    frontier_share = frontier / total

    ordered_tiers = sorted(by_tier, key=_tier_rank)
    mix = ", ".join(
        f"{by_tier[t] / total:.0%} {t} (${by_tier[t]:,.2f})" for t in ordered_tiers
    )

    models = sorted(spend, key=lambda m: (_tier_rank(tier_of[m]), -spend[m], m))
    rows = tuple(
        (
            model,
            tier_of[model],
            f"${spend[model]:,.2f}",
            f"{spend[model] / total:.0%}",
            _list_price(pricing, model),
        )
        for model in models
    )

    caveats = [
        "This check cannot tell whether any request needed the model it used. "
        "It reports the mix; sizing a routing change takes per-request "
        "analysis, which this data source cannot support.",
        _ROUTING_RESEARCH,
        "Prices differ within a tier as well as between tiers. The per-model "
        "rows carry each model's list price.",
    ]
    if _UNCLASSIFIED in by_tier:
        caveats.append(
            f"${by_tier[_UNCLASSIFIED]:,.2f} is on models the pricing file gives "
            "no tier. It is shown as unclassified rather than left out, so the "
            "shares still add up to the total."
        )

    return [
        Finding(
            check_id="C8",
            title="Model mix: where spend sits across frontier, mid and small models",
            confidence=Confidence.OBSERVATION,
            scope=f"{frontier_share:.0%} of priced spend on frontier models",
            summary=(
                f"Priced spend by tier: {mix}. This check makes no routing "
                f"claim: whether a request needed a frontier model is a property "
                f"of the request, which aggregate data never sees."
            ),
            measured_usd=frontier,
            measured_basis=(
                "priced spend on models the pricing file classifies as frontier"
            ),
            evidence=Evidence(
                columns=("model", "tier", "spend", "share", "list $/MTok in / out"),
                rows=rows,
                note=(
                    "Tiers are this tool's classification, recorded in the "
                    "pricing file -- not Anthropic's. List price is the "
                    "standard-tier rate card; the spend column is what was "
                    "actually paid, after caching, batch and routing."
                ),
            ),
            remedy=(
                "If some of this traffic is simple -- classification, "
                "extraction, short rewrites -- evaluate it on a smaller model "
                "against a sample of real requests before moving anything. "
                "That comparison needs per-request logs and a quality measure; "
                "this data has neither."
            ),
            caveats=tuple(caveats),
        )
    ]


from . import Check  # noqa: E402

CHECK = Check(
    id="C8",
    name="Model mix",
    run=run,
    # Without the model dimension every row is one blended, unpriceable
    # segment, and there is no mix to report.
    needs_dimensions=frozenset({"model"}),
)
