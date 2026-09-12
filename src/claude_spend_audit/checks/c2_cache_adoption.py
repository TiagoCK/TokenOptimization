"""C2 -- cache adoption gap. `candidate`

PROJECT_BRIEF.md §4: "High-volume segments with near-zero cache reads are
candidates. State the assumption explicitly: this only converts to savings if
the workload has a stable prompt prefix, which the aggregate data cannot show."

C1 and C2 are the same question from opposite ends. C1 finds workloads that
cache and should not; C2 finds workloads that do not cache and perhaps should.
The findings cross-reference each other, because acting on C2 without reading
C1 is how a team turns a cache-adoption win into a cache-amortization loss.

**The ceiling is a provable upper bound.** Converting an uncached input token
into a cache read saves `base − read` per read. Over a prefix of P tokens read
N times, with U = N·P total uncached tokens:

    saving = U·(base − read) − P·(write − read)

The second term is always positive -- you must pay at least one write -- so the
saving is strictly less than `U·(base − read)`. That is the figure reported,
and it is labelled as a ceiling that cannot be reached rather than as an
expectation.

**The floor is $0.** The fraction of a prompt that is a stable, reusable prefix
is a property of the prompt, which this data source never sees. Rather than
invent a percentage, the range runs from zero -- nothing is cacheable -- to the
arithmetic ceiling. `--cache-prefix-share` narrows it for someone who knows
their own workload.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from ..frame import Frame, Segment, TokenBuckets
from ..findings import Confidence, Evidence, Finding, Recoverable
from ..pricing import PricingTable, Priced
# The brief segments C2 by model and workspace.
from ._format import describe, short_tokens as _short

MTOK = Decimal("1000000")


def _hit_rate(tokens: TokenBuckets) -> Decimal:
    """cache_read / (cache_read + uncached_input + cache_creation), per §4."""
    denominator = (
        tokens.cache_read + tokens.uncached_input + tokens.cache_write_total
    )
    if denominator == 0:
        return Decimal("0")
    return Decimal(tokens.cache_read) / Decimal(denominator)


def _aggregate(frame: Frame) -> dict[Segment, TokenBuckets]:
    totals: dict[Segment, TokenBuckets] = {}
    for row in frame.usage:
        totals[row.segment] = totals.get(row.segment, TokenBuckets()) + row.tokens
    return totals


def run(frame: Frame, pricing: PricingTable, config) -> Iterable[Finding]:
    candidates: list[tuple[Segment, TokenBuckets, Decimal, Decimal]] = []

    for segment, tokens in _aggregate(frame).items():
        if tokens.uncached_input == 0:
            continue

        rate = _hit_rate(tokens)
        if rate >= config.cache_adoption_max_hit_rate:
            continue  # already caching; C1 covers whether it pays off

        base = pricing.rate_for(
            segment.model,
            "uncached_input",
            service_tier=segment.service_tier,
            inference_geo=segment.inference_geo,
            speed=segment.speed,
        )
        read = pricing.rate_for(
            segment.model,
            "cache_read",
            service_tier=segment.service_tier,
            inference_geo=segment.inference_geo,
            speed=segment.speed,
        )
        if not isinstance(base, Priced) or not isinstance(read, Priced):
            continue  # unpriceable; M1 already raised the gap

        ceiling = (
            Decimal(tokens.uncached_input)
            * (base.usd_per_mtok - read.usd_per_mtok)
            / MTOK
        )
        if ceiling >= config.min_impact_usd:
            candidates.append((segment, tokens, rate, ceiling))

    if not candidates:
        return []

    candidates.sort(key=lambda item: item[3], reverse=True)
    total_ceiling = sum((item[3] for item in candidates), Decimal("0"))

    # Roll up for display: segments are priced individually, but two that
    # differ only by geo or speed share a label. The label carries the key,
    # as C1's does: one key that never caches can sit inside a workspace that
    # caches well, and "model / workspace, 0% hit rate" would then contradict
    # every other finding about that workspace.
    rolled: dict[str, list] = {}
    for segment, tokens, rate, ceiling in candidates:
        label = describe(frame, segment, ("model", "workspace_id", "api_key_id"))
        if label in rolled:
            acc = rolled[label]
            acc[0] = acc[0] + tokens
            acc[1] += ceiling
        else:
            rolled[label] = [tokens, ceiling]

    # §3: a candidate needs a range, and the range must not rest on an invented
    # number. Zero is a real possibility -- a prompt with no stable prefix
    # cannot be cached at all -- and the ceiling is arithmetic. Anyone who knows
    # their workload can narrow it with --cache-prefix-share.
    share = getattr(config, "cache_prefix_share", None)
    low = total_ceiling * share if share is not None else Decimal("0")

    floor_clause = (
        " -- hence the floor of $0."
        if share is None
        else ", which is why the floor is an estimate rather than a measurement."
    )
    assumptions = [
        "Requires a stable prompt prefix. What fraction of these prompts repeats "
        "is a property of the prompt, which usage data never sees" + floor_clause,
        "Requires enough reuse to amortise the cache write, which costs 1.25x "
        "(5m TTL) or 2x (1h) base input. Check the C1 finding before acting: it "
        "measures workloads where that amortisation failed.",
        "The ceiling is an upper bound that cannot actually be reached, since "
        "at least one cache write must always be paid.",
    ]
    if share is not None:
        assumptions.insert(
            0,
            f"Floor assumes {share:.0%} of uncached input is a cacheable prefix "
            "(--cache-prefix-share), which is your estimate, not a measurement.",
        )

    return [
        Finding(
            check_id="C2",
            title="Segments with high uncached input and almost no cache reads",
            confidence=Confidence.CANDIDATE,
            scope=f"{len(candidates)} segment(s)",
            summary=(
                f"{len(candidates)} segment(s) read almost nothing from cache "
                f"while sending substantial uncached input. Cache reads cost "
                f"10% of base input (2.5% on Fable 5.1 and Mythos 5.1), so a "
                f"stable prefix left uncached is the single most expensive "
                f"thing a prompt can do."
            ),
            measured_usd=total_ceiling,
            measured_basis=(
                "the most caching could save on this uncached input -- an upper "
                "bound the workload cannot actually reach"
            ),
            recoverable=Recoverable(
                low=low, high=total_ceiling, assumptions=tuple(assumptions)
            ),
            evidence=Evidence(
                columns=(
                    "model / workspace / key",
                    "uncached",
                    "reads",
                    "hit rate",
                    "ceiling",
                ),
                rows=tuple(
                    (
                        label,
                        _short(tokens.uncached_input),
                        _short(tokens.cache_read),
                        f"{_hit_rate(tokens):.1%}",
                        f"${ceiling:,.2f}",
                    )
                    for label, (tokens, ceiling) in sorted(
                        rolled.items(), key=lambda kv: kv[1][1], reverse=True
                    )
                ),
                note=(
                    "Hit rate is cache_read / (cache_read + uncached_input + "
                    "cache_creation). The ceiling assumes every uncached token "
                    "becomes a cache read."
                ),
            ),
            remedy=(
                "Look for a stable prefix in these prompts -- a system prompt, a "
                "tool schema, a document header -- and put a cache breakpoint "
                "after it. Keep volatile content (timestamps, per-request IDs) "
                "after the breakpoint, since anything above it invalidates the "
                "cache on every request."
            ),
            caveats=(
                "This check cannot see prompts. A segment flagged here may have "
                "no reusable prefix at all, in which case the recoverable amount "
                "is the floor of the range, not the ceiling.",
                "If you also prune context (see C4), each change to a cached "
                "prefix costs one full cache write, once -- a transition cost, "
                "not a recurring one. Frequent edits are what break caching; "
                "C1 measures that.",
            ),
        )
    ]


from . import Check  # noqa: E402

CHECK = Check(
    id="C2",
    name="Cache adoption gap",
    run=run,
    # No dimension requirement: the arithmetic holds on an ungrouped export,
    # it just reports one blended segment. The brief segments by model and
    # workspace where those are available.
)
