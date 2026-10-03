"""C1 -- cache write amortization. `measured`

The strongest check in the tool: unambiguous, arithmetic, and the fix is a
config change. It leads the report when it finds something.

The arithmetic, per segment:

    actual         = W5·r5m + W1h·r1h + R·rread
    counterfactual = (W5 + W1h + R)·rbase      -- the same tokens, uncached
    waste          = actual − counterfactual

Every cached token was going to be processed as input either way. A cache write
processes the tokens and stores them; a cache read serves a prefix that would
otherwise have been processed fresh. So the counterfactual for the whole set is
simply all of those tokens at the base input rate, and the difference is what
caching cost or saved.

Two properties make this exact rather than an estimate:

* **No read-to-TTL attribution is required.** The usage endpoint reports
  ``cache_read_input_tokens`` as one number while splitting ``cache_creation``
  by TTL, so there is no way to know which reads came from which cache. This
  formulation never asks -- it prices what was actually billed on each side.

* **The remedy's saving is the waste itself.** Turning caching off means paying
  ``counterfactual`` instead of ``actual``, recovering exactly ``waste``. That
  is arithmetic, not an estimate, which is why C1 carries a point recoverable
  where every other check would need a range.

The TTL split is what gives this its teeth. Break-even, in
read tokens per write token, differs fourfold between the two TTLs:

    5m write at 1.25x base  ->  0.28 reads per write token
    1h write at 2x base     ->  1.11 reads per write token

A summed ``cache_creation`` bucket cannot express that, which is why a
four-bucket token model would have made this check impossible to state.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from ..frame import UNSPLIT_CACHE_WRITE, Frame, Segment, TokenBuckets
from ..findings import Confidence, Evidence, Finding, Recoverable
from ..pricing import PricingTable, Priced
from ._format import describe, short_tokens as _short

MTOK = Decimal("1000000")

_RATE_BUCKETS = ("uncached_input", "cache_write_5m", "cache_write_1h", "cache_read")


class _Rates:
    """The four rates that matter to C1, for one segment."""

    __slots__ = ("base", "write_5m", "write_1h", "read")

    def __init__(self, base: Decimal, write_5m: Decimal, write_1h: Decimal, read: Decimal):
        self.base = base
        self.write_5m = write_5m
        self.write_1h = write_1h
        self.read = read

    def cost(self, tokens: TokenBuckets) -> Decimal:
        """What the caching actually cost, as billed."""
        return (
            Decimal(tokens.cache_write_5m) * self.write_5m
            + Decimal(tokens.cache_write_1h) * self.write_1h
            + Decimal(tokens.cache_read) * self.read
        ) / MTOK

    def uncached_cost(self, tokens: TokenBuckets) -> Decimal:
        """What the same tokens would have cost with caching switched off."""
        cached_tokens = tokens.cache_write_total + tokens.cache_read
        return Decimal(cached_tokens) * self.base / MTOK


def _rates_for(pricing: PricingTable, segment: Segment) -> _Rates | None:
    """Resolve all four rates, or None if any is unavailable.

    A partially-priced segment is skipped rather than half-priced. M1 already
    raised the gap explaining why (unknown model, or an unpriceable tier).
    """
    resolved: list[Decimal] = []
    for bucket in _RATE_BUCKETS:
        rate = pricing.rate_for(
            segment.model,
            bucket,
            service_tier=segment.service_tier,
            inference_geo=segment.inference_geo,
            speed=segment.speed,
        )
        if not isinstance(rate, Priced):
            return None
        resolved.append(rate.usd_per_mtok)
    return _Rates(*resolved)


def _aggregate(frame: Frame) -> dict[Segment, TokenBuckets]:
    """Sum token buckets per full Segment.

    Grouping by the full segment rather than by (model, workspace,
    key) alone is a correctness requirement, not a preference: service tier,
    inference geo and speed all change the rates, so mixing them into one
    bucket would price the result at whichever rate happened to be looked up.
    The rollup to (model, workspace, key) happens at display time.
    """
    totals: dict[Segment, TokenBuckets] = {}
    for row in frame.usage:
        totals[row.segment] = totals.get(row.segment, TokenBuckets()) + row.tokens
    return totals


def run(frame: Frame, pricing: PricingTable, config) -> Iterable[Finding]:
    wasteful: list[tuple[Segment, TokenBuckets, _Rates, Decimal]] = []

    for segment, tokens in _aggregate(frame).items():
        if tokens.cache_write_total == 0:
            continue  # nothing was cached; C2 covers non-adoption, not C1
        rates = _rates_for(pricing, segment)
        if rates is None:
            continue

        waste = rates.cost(tokens) - rates.uncached_cost(tokens)
        if waste > config.min_impact_usd:
            wasteful.append((segment, tokens, rates, waste))

    if not wasteful:
        return []

    wasteful.sort(key=lambda item: item[3], reverse=True)
    total_waste = sum((item[3] for item in wasteful), Decimal("0"))

    # -- the remedy, bounded both ways -----------------------------------
    #
    # Rewriting 1h caches at a 5m TTL is the usual fix. Its benefit depends on
    # something the data cannot show -- whether the reuse interval is under
    # five minutes -- so it is reported as a range rather than a number:
    #
    #   best  the same reads still land, just against a cheaper write
    #   worst the shorter TTL expires before reuse and no reads land at all
    #
    # Both bounds are computed from published rates; neither is guessed.
    best = Decimal("0")
    worst = Decimal("0")
    ttl_switchable = False

    for segment, tokens, rates, waste in wasteful:
        if tokens.cache_write_1h == 0:
            continue
        ttl_switchable = True
        all_writes = Decimal(tokens.cache_write_total)

        reads_land = (
            all_writes * rates.write_5m + Decimal(tokens.cache_read) * rates.read
        ) / MTOK - rates.uncached_cost(tokens)
        reads_lost = (
            all_writes * rates.write_5m / MTOK
            - Decimal(tokens.cache_write_total) * rates.base / MTOK
        )
        best += waste - reads_land
        worst += waste - reads_lost

    low, high = (worst, best) if worst <= best else (best, worst)

    evidence = Evidence(
        # Six columns, deliberately. Terminal output gets pasted into email,
        # and a table that wraps is a table nobody reads. Exact token counts
        # are in the --json output; this is the shape of the problem.
        columns=("model / workspace / key", "5m wr", "1h wr", "reads", "r:w", "waste"),
        rows=tuple(
            (
                # Labelled by (model, workspace, key), the identity a reader knows.
                describe(frame, segment, ("model", "workspace_id", "api_key_id")),
                _short(tokens.cache_write_5m),
                _short(tokens.cache_write_1h),
                _short(tokens.cache_read),
                _ratio(tokens),
                f"${waste:,.2f}",
            )
            for segment, tokens, rates, waste in wasteful
        ),
        note=(
            "'r:w' is cache-read tokens per cache-write token. Break-even is "
            "0.28 for the 5-minute TTL and 1.11 for the 1-hour one, so the "
            "same ratio can be healthy or wasteful depending on the mix."
        ),
    )

    shortfall = sum(
        (_extra_reads_needed(rates, waste) for _, _, rates, waste in wasteful), 0
    )

    remedy_parts = [
        "Either raise the read rate on these prefixes -- roughly "
        f"{shortfall:,} more cache-read tokens would reach break-even at "
        "current volumes -- or stop caching the segments that cannot get "
        "there.",
    ]
    if ttl_switchable:
        remedy_parts.append(
            "Traffic on the 1-hour TTL is the usual culprit: it costs 2x base "
            "input to write against 1.25x for the 5-minute TTL, so it needs "
            "four times the reuse to justify itself. Moving it to the "
            f"5-minute TTL would have cost ${low:,.2f} to ${high:,.2f} less "
            "over this period."
        )

    return [
        Finding(
            check_id="C1",
            title="Cache writes are not being read back often enough to pay for themselves",
            confidence=Confidence.MEASURED,
            scope=f"{len(wasteful)} segment(s)",
            summary=(
                f"{len(wasteful)} segment(s) paid more for prompt caching than "
                "the same tokens would have cost uncached. Cache writes carry a "
                "premium over base input (1.25x for the 5-minute TTL, 2x for the "
                "1-hour), and that premium is only recovered by subsequent reads."
            ),
            measured_usd=total_waste,
            measured_basis=(
                "cache premium paid and not recovered, versus the same tokens "
                "billed as uncached input"
            ),
            recoverable=Recoverable.exactly(
                total_waste,
                "Disabling caching on these segments recovers this exactly: the "
                "same tokens would bill as uncached input at the base rate.",
                "Cache reads are also faster than uncached input, so this trades "
                "latency for cost. Where latency matters, fix the reuse rate or "
                "the TTL instead of switching caching off.",
            ),
            evidence=evidence,
            remedy=" ".join(remedy_parts),
            caveats=(
                "Figures are net across the whole period. A segment that cached "
                "well most days and badly on a few nets out here; the per-segment "
                "rows above show where the loss actually sits.",
                "A high write-to-read ratio usually means a cache TTL shorter "
                "than the real request interval, or a prompt prefix that varies "
                "when it should not.",
            ),
        )
    ]


def _ratio(tokens: TokenBuckets) -> str:
    if tokens.cache_write_total == 0:
        return "-"
    return f"{Decimal(tokens.cache_read) / Decimal(tokens.cache_write_total):.2f}"


def _breakeven_ratio(rates: _Rates) -> str:
    """Reads per write token needed for this segment's TTL mix to break even.

    Solved from waste == 0. Uses the segment's own resolved rates, so the
    Fable 5.1 cache-read exception and the geo multiplier are handled without
    special-casing.
    """
    denominator = rates.base - rates.read
    if denominator <= 0:  # pragma: no cover - would be a pathological rate card
        return "n/a"
    return f"{(rates.write_1h - rates.base) / denominator:.2f}"


def _extra_reads_needed(rates: _Rates, waste: Decimal) -> int:
    """Read tokens that would have to be added to reach break-even.

    Each additional read token replaces a token that would have been billed at
    the base rate, so it closes the gap by (base − read) per token.
    """
    denominator = rates.base - rates.read
    if denominator <= 0:  # pragma: no cover
        return 0
    return int(waste / denominator * MTOK)


from . import Check  # noqa: E402  (circular by design: the registry owns the type)

CHECK = Check(
    id="C1",
    name="Cache write amortization",
    run=run,
    # Deliberately no dimension requirement. C1 is arithmetic on token buckets
    # and stays correct on an ungrouped export -- it just reports one blended
    # segment instead of many.
    #
    # But it cannot run at all on a source that summed the cache TTLs. The
    # entire check is the difference between a 1.25x write and a 2x one; given
    # only their sum it would have to assume a TTL, and would understate waste
    # on 1h-heavy workloads with no visible sign that it had guessed.
    blocked_by_tokens=frozenset({UNSPLIT_CACHE_WRITE}),
    # And it needs the columns at all: an export with no cache columns has
    # zero writes in every row, which is not the same as writing nothing.
    needs_tokens=frozenset({"cache_read", "cache_write_5m", "cache_write_1h"}),
)
