"""The internal representation both ingest paths normalize into.

Checks never learn which path the data came from, per PROJECT_BRIEF.md §3.

Field names deliberately mirror the Usage & Cost API's own vocabulary so that
nothing here is invented. In particular the five token buckets are exactly the
five values of the cost endpoint's ``token_type`` enum -- see TOKEN_TYPE_TO_FIELD.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Iterator, Mapping

from .gaps import Gap

# --------------------------------------------------------------------------
# Vocabulary lifted verbatim from the API reference
# --------------------------------------------------------------------------

#: Maps the cost endpoint's ``token_type`` enum onto TokenBuckets attributes.
#: The brief describes four buckets; the API has five, because cache creation
#: is an object split by TTL and the two TTLs are priced differently.
TOKEN_TYPE_TO_FIELD: Mapping[str, str] = {
    "uncached_input_tokens": "uncached_input",
    "cache_read_input_tokens": "cache_read",
    "cache_creation.ephemeral_5m_input_tokens": "cache_write_5m",
    "cache_creation.ephemeral_1h_input_tokens": "cache_write_1h",
    "output_tokens": "output",
}

TOKEN_FIELDS: tuple[str, ...] = tuple(TOKEN_TYPE_TO_FIELD.values())

#: A sixth bucket with no API counterpart. Some exporters -- notably the
#: official cookbook's `export_usage_to_csv` -- sum the two cache-creation TTLs
#: into one `cache_creation_tokens` column, destroying a distinction that
#: cannot be recovered afterwards. Tokens landing here are real spend, but C1
#: cannot run on them: its whole value is the 4x difference between the TTLs.
#:
#: Kept out of TOKEN_FIELDS so that tuple keeps meaning "the five the API
#: reports", and so no code paths it through a token_type lookup.
UNSPLIT_CACHE_WRITE = "cache_write_unsplit"

#: Every bucket, including the lossy one. Used for arithmetic and display.
ALL_TOKEN_FIELDS: tuple[str, ...] = TOKEN_FIELDS + (UNSPLIT_CACHE_WRITE,)

#: Human labels for report headers (§5 wants tokens broken out by category).
TOKEN_FIELD_LABELS: Mapping[str, str] = {
    "uncached_input": "uncached input",
    "cache_read": "cache read",
    "cache_write_5m": "cache write (5m)",
    "cache_write_1h": "cache write (1h)",
    "output": "output",
    UNSPLIT_CACHE_WRITE: "cache write (TTL unknown)",
}

#: Every dimension the usage endpoint can group by. Order is stable for output.
DIMENSIONS: tuple[str, ...] = (
    "model",
    #: Claude product surface (chat, claude_code, cowork...). Only the Claude
    #: Enterprise Analytics API reports it; the Usage API and Console exports
    #: never group by it, so it stays out of their grouped_by.
    "product",
    "workspace_id",
    "api_key_id",
    "account_id",
    "service_account_id",
    "service_tier",
    "context_window",
    "inference_geo",
    "speed",
)

SERVICE_TIERS: frozenset[str] = frozenset(
    {"standard", "batch", "flex", "flex_discount", "priority", "priority_on_demand"}
)
CONTEXT_WINDOWS: frozenset[str] = frozenset({"0-200k", "200k-1M"})
#: The long-context bucket, for C4 and the long-context pricing gap.
LONG_CONTEXT_WINDOW = "200k-1M"
INFERENCE_GEOS: frozenset[str] = frozenset({"global", "us", "not_available"})
SPEEDS: frozenset[str] = frozenset({"standard", "fast"})
COST_TYPES: frozenset[str] = frozenset(
    {"tokens", "web_search", "code_execution", "session_usage"}
)
BUCKET_WIDTHS: frozenset[str] = frozenset({"1m", "1h", "1d"})


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenBuckets:
    """The five token categories, kept separate throughout.

    Summing cache_write_5m and cache_write_1h would destroy C1: the two TTLs
    carry different premiums (1.25x vs 2x base input), so they have different
    break-even points (1 read vs 2 reads).
    """

    uncached_input: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    output: int = 0
    #: Cache writes from a source that summed the TTLs. See UNSPLIT_CACHE_WRITE.
    cache_write_unsplit: int = 0

    @property
    def cache_write_total(self) -> int:
        return self.cache_write_5m + self.cache_write_1h + self.cache_write_unsplit

    @property
    def has_unsplit_cache_writes(self) -> bool:
        """True when this data cannot support C1."""
        return self.cache_write_unsplit > 0

    @property
    def input_total(self) -> int:
        """All input-side tokens, however they were billed."""
        return self.uncached_input + self.cache_read + self.cache_write_total

    @property
    def total(self) -> int:
        return self.input_total + self.output

    def __add__(self, other: TokenBuckets) -> TokenBuckets:
        if not isinstance(other, TokenBuckets):
            return NotImplemented
        return TokenBuckets(
            **{f: getattr(self, f) + getattr(other, f) for f in ALL_TOKEN_FIELDS}
        )

    def as_dict(self) -> dict[str, int]:
        return {f: getattr(self, f) for f in ALL_TOKEN_FIELDS}


@dataclass(frozen=True, slots=True)
class Segment:
    """The grouping key for a usage row.

    ``None`` is not simply "missing" here. Read it together with
    ``Frame.grouped_by``: inside a grouped dimension it carries the API's own
    meaning (default workspace, or Console playground traffic with no API key);
    outside one it means the source never broke that dimension out at all.
    ``Frame.value_of`` enforces that distinction so callers cannot get it wrong.
    """

    model: str | None = None
    product: str | None = None
    workspace_id: str | None = None
    api_key_id: str | None = None
    account_id: str | None = None
    service_account_id: str | None = None
    service_tier: str | None = None
    context_window: str | None = None
    inference_geo: str | None = None
    speed: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {d: getattr(self, d) for d in DIMENSIONS}


@dataclass(frozen=True, slots=True)
class UsageRow:
    """One time bucket x one grouping combination from the usage endpoint."""

    start: datetime
    end: datetime
    segment: Segment = field(default_factory=Segment)
    tokens: TokenBuckets = field(default_factory=TokenBuckets)
    web_search_requests: int = 0


@dataclass(frozen=True, slots=True)
class CostRow:
    """One line from the cost endpoint. ``amount_usd`` is already in dollars.

    The API returns ``amount`` as a decimal string in the *lowest currency unit*
    -- "123.45" in USD means $1.23, not $123.45. The conversion happens once, at
    ingest, so nothing downstream can be off by 100x.
    """

    start: datetime
    end: datetime
    amount_usd: Decimal
    currency: str = "USD"
    cost_type: str | None = None
    description: str | None = None
    token_type: str | None = None
    workspace_id: str | None = None
    model: str | None = None
    service_tier: str | None = None
    context_window: str | None = None
    inference_geo: str | None = None
    product: str | None = None
    #: The list-price (pre-discount) amount, in dollars, when the source
    #: reports one -- only the Enterprise Analytics cost report does.
    list_amount_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """Where a Frame came from, so the report header can say so."""

    path: str
    ingest_path: str  # "csv" | "api"
    profile: str | None = None  # schema profile name, for the CSV path
    profile_status: str | None = None  # "provisional" | "verified"
    unmapped_columns: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# Frame
# --------------------------------------------------------------------------

#: Sentinel for "this dimension was never broken out by the source", as
#: distinct from None, which means the API's own null (default workspace,
#: playground traffic). Returned by Frame.value_of.
NOT_GROUPED = "<not grouped>"


@dataclass(frozen=True, slots=True)
class Frame:
    """Normalized usage and cost data plus everything known to be missing.

    ``grouped_by`` is the load-bearing field. Without it, a Console export that
    simply was not broken out by workspace is indistinguishable from one where
    every request genuinely hit the default workspace -- and C7 would report
    100% unattributed spend on data that says no such thing.
    """

    usage: tuple[UsageRow, ...] = ()
    cost: tuple[CostRow, ...] = ()
    grouped_by: frozenset[str] = frozenset()
    bucket_width: str | None = None
    sources: tuple[SourceInfo, ...] = ()
    gaps: tuple[Gap, ...] = ()
    #: Token buckets the source actually reported, read the same way as
    #: ``grouped_by``: a CSV with no cache-read column has zero cache reads
    #: in every row, and only this field says that zero was never measured.
    #: None means the source always reports every bucket (the API paths).
    token_fields: frozenset[str] | None = None

    # -- period ----------------------------------------------------------

    @property
    def period_start(self) -> datetime | None:
        starts = [r.start for r in self.usage] + [r.start for r in self.cost]
        return min(starts) if starts else None

    @property
    def period_end(self) -> datetime | None:
        ends = [r.end for r in self.usage] + [r.end for r in self.cost]
        return max(ends) if ends else None

    def has_token_field(self, name: str) -> bool:
        """True when the source carried this token bucket at all."""
        return self.token_fields is None or name in self.token_fields

    # -- dimensions ------------------------------------------------------

    def value_of(self, segment: Segment, dimension: str) -> str | None:
        """Read a dimension off a segment without conflating null with absent.

        Returns ``NOT_GROUPED`` when the source never carried this dimension,
        ``None`` when the source carried it and the API's value was genuinely
        null (default workspace / Console playground), otherwise the value.
        """
        if dimension not in DIMENSIONS:
            raise KeyError(f"unknown dimension {dimension!r}")
        if dimension not in self.grouped_by:
            return NOT_GROUPED
        return getattr(segment, dimension)

    def has_dimension(self, dimension: str) -> bool:
        return dimension in self.grouped_by

    def distinct(self, dimension: str) -> tuple[str | None, ...]:
        """Distinct values seen for a grouped dimension, in first-seen order."""
        if not self.has_dimension(dimension):
            return ()
        seen: dict[str | None, None] = {}
        for row in self.usage:
            seen.setdefault(getattr(row.segment, dimension), None)
        return tuple(seen)

    # -- totals ----------------------------------------------------------

    @property
    def tokens(self) -> TokenBuckets:
        total = TokenBuckets()
        for row in self.usage:
            total = total + row.tokens
        return total

    @property
    def web_search_requests(self) -> int:
        return sum(r.web_search_requests for r in self.usage)

    @property
    def billed_total_usd(self) -> Decimal | None:
        """Sum of the cost endpoint's own figures, or None if no cost data.

        This is what was actually billed, as far as the cost endpoint reports
        it. It is *not* authoritative when Priority Tier traffic is present --
        see the PRIORITY_TIER_PRESENT gap.
        """
        if not self.cost:
            return None
        return sum((r.amount_usd for r in self.cost), Decimal("0"))

    @property
    def list_total_usd(self) -> Decimal | None:
        """Sum of the cost data's list-price amounts, or None if it has none.

        Only the Enterprise Analytics cost report carries a list price beside
        the billed amount. Every other source leaves this None, so nothing in
        their reports changes.
        """
        amounts = [r.list_amount_usd for r in self.cost if r.list_amount_usd is not None]
        if not amounts:
            return None
        return sum(amounts, Decimal("0"))

    # -- construction helpers -------------------------------------------

    def with_gaps(self, new_gaps: Iterable[Gap]) -> Frame:
        merged = list(self.gaps)
        known = {(g.code, g.scope) for g in merged}
        for gap in new_gaps:
            if (gap.code, gap.scope) not in known:
                merged.append(gap)
                known.add((gap.code, gap.scope))
        return replace(self, gaps=tuple(merged))

    def __iter__(self) -> Iterator[UsageRow]:
        return iter(self.usage)


def merge(*frames: Frame) -> Frame:
    """Combine frames from separate files (e.g. a usage export and a cost export).

    ``grouped_by`` intersects rather than unions: a dimension is only usable for
    analysis if every contributing source carried it. Taking the union would let
    a check read a dimension off rows that never had one.
    """
    present = [f for f in frames if f is not None]
    if not present:
        return Frame()
    if len(present) == 1:
        return present[0]

    usage: list[UsageRow] = []
    cost: list[CostRow] = []
    sources: list[SourceInfo] = []
    grouped: frozenset[str] | None = None
    tokens: frozenset[str] | None = None
    widths: set[str] = set()

    for frame in present:
        usage.extend(frame.usage)
        cost.extend(frame.cost)
        sources.extend(frame.sources)
        if frame.usage:
            # Only sources that carry usage rows constrain the dimension set;
            # a cost-only export has no segments to speak for.
            grouped = frame.grouped_by if grouped is None else grouped & frame.grouped_by
            # Same rule for token buckets; None (all of them) is the identity.
            if frame.token_fields is not None:
                tokens = (
                    frame.token_fields if tokens is None else tokens & frame.token_fields
                )
        if frame.bucket_width:
            widths.add(frame.bucket_width)

    merged = Frame(
        usage=tuple(usage),
        cost=tuple(cost),
        grouped_by=grouped or frozenset(),
        bucket_width=widths.pop() if len(widths) == 1 else None,
        sources=tuple(sources),
        token_fields=tokens,
    )
    for frame in present:
        merged = merged.with_gaps(frame.gaps)
    return merged
