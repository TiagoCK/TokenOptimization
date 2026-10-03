"""C4 -- long-context composition. `observation`

Reports the `200k-1M` bucket: spend, share of total, and the split across
the five token buckets, pointing the reader at the uncached share.

**There is no long-context premium to recover.** The first design of this check
assumed one. The pricing page now says Claude 4.6 and later "include the full
1M token context window at standard pricing" -- a 900k-token request costs the
same per token as a 9k one. So moving work below 200k saves exactly the tokens
removed, at the ordinary rate, and nothing more.

What survives is that context still drives spend, and that **not every token
in it is worth the same to remove**. A cached token costs a tenth of an
uncached one (a fortieth on Fable 5.1 and Mythos 5.1). This check measures
that directly: for each token category in the long-context window it reports
the effective rate actually paid -- spend divided by tokens -- which is what
one fewer token in that category would have saved at this workload's model,
tier and routing mix. The ratio between categories falls out of the data; no
multiplier is written here.

**It never estimates a saving.** How many tokens are removable is a property
of the prompt, which this data never sees. A `candidate` would need a
prunable-fraction assumption, and any number put there would be invented. So
this is an `observation`: the rate per token is measured, and the reader who
knows their prompts does the multiplication.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from ..frame import (
    ALL_TOKEN_FIELDS,
    LONG_CONTEXT_WINDOW,
    TOKEN_FIELD_LABELS,
    Frame,
)
from ..findings import Confidence, Evidence, Finding
from ..pricing import TOKENS_PER_MTOK, PricingTable
from ._format import multiple, short_tokens


def run(frame: Frame, pricing: PricingTable, config) -> Iterable[Finding]:
    total = Decimal("0")
    tokens: dict[str, int] = {}
    spend: dict[str, Decimal] = {}
    #: Canonical model -> long-context tokens, for models where the pricing
    #: page does not confirm the standard rate applies.
    unconfirmed: dict[str, int] = {}

    for row in frame.usage:
        costs = pricing.bucket_costs(row.tokens, row.segment)
        if not costs:
            continue  # unpriced; the estimate already raised a gap for it
        total += sum(costs.values(), Decimal("0"))

        if row.segment.context_window != LONG_CONTEXT_WINDOW:
            continue
        for bucket, usd in costs.items():
            # Only priced buckets are counted, so every effective rate below
            # divides priced spend by the tokens that produced it.
            tokens[bucket] = tokens.get(bucket, 0) + getattr(row.tokens, bucket)
            spend[bucket] = spend.get(bucket, Decimal("0")) + usd
        if not pricing.long_context_confirmed(row.segment.model):
            label = pricing.resolve_model(row.segment.model) or str(row.segment.model)
            unconfirmed[label] = unconfirmed.get(label, 0) + sum(
                getattr(row.tokens, b) for b in costs
            )

    long_spend = sum(spend.values(), Decimal("0"))
    if total <= 0 or long_spend <= 0:
        return []
    share = long_spend / total
    if share < config.long_context_min_share or long_spend < config.min_impact_usd:
        return []

    def rate(bucket: str) -> Decimal | None:
        count = tokens.get(bucket, 0)
        if not count:
            return None
        return spend[bucket] * TOKENS_PER_MTOK / Decimal(count)

    uncached_rate = rate("uncached_input")
    rows: list[tuple[str, ...]] = []
    for bucket in ALL_TOKEN_FIELDS:
        if not tokens.get(bucket):
            continue
        label = TOKEN_FIELD_LABELS[bucket]
        if bucket == "output":
            label += " (not context)"
        bucket_rate = rate(bucket)
        rows.append(
            (
                label,
                short_tokens(tokens[bucket]),
                f"${spend[bucket]:,.2f}",
                f"{spend[bucket] / long_spend:.0%}",
                f"${bucket_rate:,.2f}",
                multiple(bucket_rate / uncached_rate) if uncached_rate else "-",
            )
        )

    uncached_share = spend.get("uncached_input", Decimal("0")) / long_spend
    read_rate = rate("cache_read")
    leverage = (
        f" A token already in a cached prefix is worth {multiple(read_rate / uncached_rate)} "
        f"as much to remove, so that is usually the wrong place to start."
        if read_rate is not None and uncached_rate
        else ""
    )

    caveats = [
        "There is no long-context premium to recover. On Claude 4.6 and later "
        "the pricing page bills a 900k-token request at the same per-token rate "
        "as a 9k one, so moving work below 200k saves only the tokens removed, "
        "at the ordinary rate.",
        "Changing a cached prefix invalidates it once: the next request pays a "
        "full cache write, and later requests write the smaller prefix. That is "
        "a one-time transition cost. The hazard is churn -- edits frequent "
        "enough that the write never pays for itself -- which C1 measures.",
        "Claude 4.7 and later use a tokenizer that produces roughly 30% more "
        "tokens for the same text. A rise in long-context share across a model "
        "migration may be the tokenizer rather than the content.",
        "Long context is frequently load-bearing. Fewer tokens is not "
        "automatically better, and this check makes no claim that it is.",
    ]
    if unconfirmed:
        named = ", ".join(
            f"{model} ({count:,} tokens)" for model, count in sorted(unconfirmed.items())
        )
        caveats.append(
            f"Some of this spend is on models older than Claude 4.6 -- {named} -- "
            "where the pricing page does not confirm that long context is billed "
            "at the standard rate. It is priced at that rate here, and may be "
            "understated."
        )

    return [
        Finding(
            check_id="C4",
            title="Spend on requests with more than 200k tokens of context",
            confidence=Confidence.OBSERVATION,
            scope=f"{share:.0%} of priced spend",
            summary=(
                f"${long_spend:,.2f} ({share:.0%} of priced spend) went on requests "
                f"in the 200k-1M context window. {uncached_share:.0%} of that was "
                f"uncached input, where each token removed saves the full input "
                f"rate.{leverage} This check estimates no saving: how much of the "
                f"context is removable is a property of the prompts, which usage "
                f"data never sees."
            ),
            measured_usd=long_spend,
            measured_basis="priced spend on requests in the 200k-1M context window",
            evidence=Evidence(
                columns=("category", "tokens", "spend", "share", "$/MTok", "vs uncached"),
                rows=tuple(rows),
                note=(
                    "$/MTok is spend divided by tokens for each category in this "
                    "window, so it already reflects the models, tiers and routing "
                    "actually used: it is what a million fewer tokens there would "
                    "have saved. Output is generated rather than sent, and is "
                    "listed so the spend column adds up to the total."
                ),
            ),
            remedy=(
                "Start with the uncached input. Retrieval that sends fewer, "
                "better-chosen passages, or trimming conversation history, removes "
                "tokens at the full rate. Context that is stable across requests "
                "is better cached than cut -- C2 sizes that. Tokens already in a "
                "cached prefix are the least valuable to remove."
            ),
            caveats=tuple(caveats),
        )
    ]


from . import Check  # noqa: E402

CHECK = Check(
    id="C4",
    name="Long-context composition",
    run=run,
    # Without context_window every row is an unknown, and treating unknown as
    # "short" would report a confident 0% -- a number the data never gave.
    needs_dimensions=frozenset({"context_window"}),
)
