"""C5 -- output token share. `observation`

PROJECT_BRIEF.md §4: "Output is several times the price of input per token. An
unusually high output share can indicate missing `max_tokens` discipline or
verbose response formats. Observation only -- some workloads are legitimately
generative."

**The threshold is a share of tokens, not of spend.** Spend share looks like
the natural measure and is the wrong one: caching makes input cheaper without
changing how many tokens there are, so a well-cached agent's output share of
spend rises precisely *because* it did the right thing. Judged on spend, C5
would flag the workloads C1 and C2 approve of. Token share is untouched by
caching, so it isolates what the brief is asking about -- how much a workload
writes relative to what it reads. Spend share is still reported beside it,
because that is the figure that costs money.

**"Several times the price" is read from the rate card**, per model, rather
than written here. Today it is exactly 5x on every model on file; a check that
said "5x" as a literal would go quietly wrong the day that changed.

Two things this data cannot tell apart, and the finding says so:

* Output includes thinking. It is billed as output, and the usage report has
  no field separating it from the visible answer.
* There are no request counts, so ten long responses and a thousand short
  ones look the same -- which is the very distinction `max_tokens` discipline
  is about.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from ..frame import Frame, TokenBuckets
from ..findings import Confidence, Evidence, Finding
from ..pricing import PricingTable, Priced
from ._format import describe, multiple, short_tokens


def _output_multiple(pricing: PricingTable, model: str | None) -> Decimal | None:
    """Output rate over uncached input rate, straight from the rate card."""
    output = pricing.rate_for(model, "output")
    uncached = pricing.rate_for(model, "uncached_input")
    if not isinstance(output, Priced) or not isinstance(uncached, Priced):
        return None
    if uncached.usd_per_mtok == 0:
        return None
    return output.usd_per_mtok / uncached.usd_per_mtok


def run(frame: Frame, pricing: PricingTable, config) -> Iterable[Finding]:
    # Rolled up by model / workspace for display, like C2 and C3. Pricing is
    # per full segment, because tier, geo and speed all change the rates.
    tokens: dict[str, TokenBuckets] = {}
    spend: dict[str, Decimal] = {}
    output_spend: dict[str, Decimal] = {}
    models: dict[str, set[str | None]] = {}

    for row in frame.usage:
        costs = pricing.bucket_costs(row.tokens, row.segment)
        if not costs:
            continue  # unpriced; the estimate already raised a gap for it
        label = describe(frame, row.segment)
        # Count only the buckets that were priced, so the token share and the
        # spend share describe the same tokens.
        priced = TokenBuckets(**{b: getattr(row.tokens, b) for b in costs})
        tokens[label] = tokens.get(label, TokenBuckets()) + priced
        spend[label] = spend.get(label, Decimal("0")) + sum(costs.values(), Decimal("0"))
        output_spend[label] = output_spend.get(label, Decimal("0")) + costs.get(
            "output", Decimal("0")
        )
        models.setdefault(label, set()).add(
            pricing.resolve_model(row.segment.model) or row.segment.model
        )

    threshold = config.output_min_token_share
    flagged: list[str] = []
    for label, counts in tokens.items():
        if counts.total == 0:
            continue
        share = Decimal(counts.output) / Decimal(counts.total)
        if share >= threshold and output_spend[label] >= config.min_impact_usd:
            flagged.append(label)

    if not flagged:
        return []
    flagged.sort(key=lambda label: output_spend[label], reverse=True)

    flagged_output = sum((output_spend[l] for l in flagged), Decimal("0"))
    flagged_spend = sum((spend[l] for l in flagged), Decimal("0"))
    flagged_tokens = sum((tokens[l] for l in flagged), TokenBuckets())
    token_share = Decimal(flagged_tokens.output) / Decimal(flagged_tokens.total)
    spend_share = flagged_output / flagged_spend

    involved = {model for label in flagged for model in models[label]}
    multiples = sorted(
        {m for model in involved if (m := _output_multiple(pricing, model)) is not None}
    )
    if not multiples:
        price_clause = ""
    else:
        costs_what = (
            multiple(multiples[0])
            if len(multiples) == 1
            else f"between {multiple(multiples[0])} and {multiple(multiples[-1])}"
        )
        price_clause = (
            f" Output costs {costs_what} as much as uncached input on "
            f"{'this model' if len(involved) == 1 else 'these models'}, so it "
            f"dominates the bill long before it dominates the token count."
        )

    rows = tuple(
        (
            label,
            short_tokens(tokens[label].output),
            f"{Decimal(tokens[label].output) / Decimal(tokens[label].total):.0%}",
            f"${output_spend[label]:,.2f}",
            f"{output_spend[label] / spend[label]:.0%}",
        )
        for label in flagged
    )

    return [
        Finding(
            check_id="C5",
            title="Segments where output is most of the bill",
            confidence=Confidence.OBSERVATION,
            scope=f"{len(flagged)} segment(s)",
            summary=(
                f"In {len(flagged)} segment(s), output is {token_share:.0%} of "
                f"tokens but {spend_share:.0%} of spend (${flagged_output:,.2f})."
                f"{price_clause} That can mean responses longer than anyone "
                f"reads, or it can be the job -- this data cannot say which, and "
                f"this check claims no saving."
            ),
            measured_usd=flagged_output,
            measured_basis=(
                "spend on output tokens in the segments shown -- what was spent, "
                "not what could be saved"
            ),
            evidence=Evidence(
                columns=("model / workspace", "output", "of tokens", "output spend", "of spend"),
                rows=rows,
                note=(
                    f"Shown because output is at least {threshold:.0%} of the "
                    "segment's tokens. The threshold uses tokens rather than "
                    "spend because caching makes input cheaper without changing "
                    "how many tokens there are: judged on spend, a well-cached "
                    "workload would look output-heavy for having done the right "
                    "thing."
                ),
            ),
            remedy=(
                "Check whether responses are longer than whatever consumes them "
                "needs. Set max_tokens to the length actually used, ask for terse "
                "or structured output where no person reads it, and on models "
                "with adaptive thinking try a lower effort level for requests "
                "that do not need deep reasoning. "
                "usage.output_tokens_details.thinking_tokens on a sample of "
                "responses shows how much of the output is reasoning. Changing "
                "effort invalidates the prompt cache once, like any change to "
                "the thinking configuration."
            ),
            caveats=(
                "Output tokens include thinking, which is billed as output. The "
                "usage report does not separate the two, so a high share may be "
                "reasoning rather than long answers.",
                "Usage data carries no request counts, so this cannot tell ten "
                "long responses from a thousand short ones -- the distinction "
                "max_tokens discipline is about.",
                "Some workloads are legitimately generative: drafting, code "
                "generation and translation write more than they read. This is "
                "context, not a verdict.",
            ),
        )
    ]


from . import Check  # noqa: E402

CHECK = Check(
    id="C5",
    name="Output token share",
    run=run,
    # No dimension requirement: on an ungrouped export this reports one
    # blended segment, which is still a true statement about the whole.
)
