"""The data model for a Team/Enterprise plan spend report.

Deliberately not a ``Frame``. A Frame is time buckets of five token
categories, and every check written against it reads a missing bucket as
zero. This report has no time buckets and no cache-write split, so folding it
into a Frame would hand C1 a "clean" result, C2 a model that never caches and
C6 a flat month -- each a finding the data never supported.

So it gets its own shape, and the same rule the Frame lives by: a column the
export does not carry is ``None``, never ``0``. ``SpendReport.present`` is the
counterpart of ``Frame.grouped_by``, and checks declare which fields they need
against it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Iterable

from ..frame import SourceInfo
from ..gaps import Gap

#: Text columns: who, on what, with which model.
TEXT_FIELDS: tuple[str, ...] = (
    "user_email",
    "account_uuid",
    "user_name",
    "product",
    "model",
    "model_family",
    "currency",
)
#: Counts.
INT_FIELDS: tuple[str, ...] = (
    "requests",
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "web_search_count",
)
#: Dollars, as the report writes them. Never re-priced.
SPEND_FIELDS: tuple[str, ...] = ("net_spend_usd", "gross_spend_usd")

ALL_FIELDS: tuple[str, ...] = TEXT_FIELDS + INT_FIELDS + SPEND_FIELDS

#: Human labels for report output.
FIELD_LABELS: dict[str, str] = {
    "requests": "requests",
    "prompt_tokens": "prompt tokens",
    "completion_tokens": "completion tokens",
    "cache_read_tokens": "cache read tokens",
    "cache_write_tokens": "cache write tokens",
    "web_search_count": "web searches",
    "net_spend_usd": "net spend",
    "gross_spend_usd": "gross spend",
}

#: Pseudo-field a check can list in ``needs_fields``: "some spend column".
SPEND = "spend"


@dataclass(frozen=True, slots=True)
class SpendRow:
    """One person's usage of one model across the export's date range.

    Every field is optional. ``None`` means the column is not in the export;
    a column that is present but blank in this row parses as ``0`` (counts and
    dollars) or ``None`` (text), exactly as the CSV path does for usage data.
    """

    user_email: str | None = None
    account_uuid: str | None = None
    user_name: str | None = None
    product: str | None = None
    model: str | None = None
    model_family: str | None = None
    currency: str | None = None

    requests: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    web_search_count: int | None = None

    net_spend_usd: Decimal | None = None
    gross_spend_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SpendReport:
    """A whole spend report, plus what it is known not to contain."""

    rows: tuple[SpendRow, ...] = ()
    #: Fields the export actually carries. Read this, never a row's None.
    present: frozenset[str] = frozenset()
    #: Only set when the file itself states its range. The documented export
    #: has no date column: the range is whatever was chosen when exporting.
    period_start: datetime | None = None
    period_end: datetime | None = None
    sources: tuple[SourceInfo, ...] = ()
    gaps: tuple[Gap, ...] = ()

    # -- fields ----------------------------------------------------------

    def has(self, name: str) -> bool:
        """Whether the export carries ``name``. ``"spend"`` means either column."""
        if name == SPEND:
            return self.spend_field is not None
        return name in self.present

    @property
    def spend_field(self) -> str | None:
        """The spend column analysis reads: net if present, else gross.

        Net is what is actually paid, so it is preferred. Every finding names
        which one it used in its ``measured_basis``.
        """
        for name in SPEND_FIELDS:
            if name in self.present:
                return name
        return None

    @property
    def spend_label(self) -> str:
        return FIELD_LABELS.get(self.spend_field or "", "spend")

    def spend_of(self, row: SpendRow) -> Decimal:
        """A row's spend in the chosen column. Zero only for a blank cell."""
        name = self.spend_field
        if name is None:
            raise ValueError("this spend report carries no spend column")
        value = getattr(row, name)
        return value if value is not None else Decimal("0")

    # -- totals ----------------------------------------------------------

    def total(self, name: str) -> int | Decimal | None:
        """Sum of a numeric field, or None when the export lacks it."""
        if name not in self.present:
            return None
        zero: int | Decimal = Decimal("0") if name in SPEND_FIELDS else 0
        return sum((getattr(r, name) or zero for r in self.rows), zero)

    @property
    def spend_total(self) -> Decimal | None:
        name = self.spend_field
        return None if name is None else self.total(name)  # type: ignore[return-value]

    def distinct(self, name: str) -> tuple[str, ...]:
        """Distinct non-null values of a text field, in first-seen order."""
        if name not in self.present:
            return ()
        seen: dict[str, None] = {}
        for row in self.rows:
            value = getattr(row, name)
            if value is not None:
                seen.setdefault(value, None)
        return tuple(seen)

    # -- construction helpers -------------------------------------------

    def with_gaps(self, new_gaps: Iterable[Gap]) -> SpendReport:
        merged = list(self.gaps)
        known = {(g.code, g.scope) for g in merged}
        for gap in new_gaps:
            if (gap.code, gap.scope) not in known:
                merged.append(gap)
                known.add((gap.code, gap.scope))
        return replace(self, gaps=tuple(merged))

    def anonymized(self) -> SpendReport:
        """Replace every email with ``user-001``... and drop UUIDs and names.

        Numbered by spend, largest first, so ``user-001`` is the heaviest
        spender in every table. Ties, and reports with no spend column, fall
        back to tokens and then to the email itself, so the numbering is
        deterministic. The same person gets the same label everywhere.
        """
        weight: dict[str, tuple[Decimal, int]] = {}
        for row in self.rows:
            if row.user_email is None:
                continue
            spend = self.spend_of(row) if self.spend_field else Decimal("0")
            tokens = (row.prompt_tokens or 0) + (row.completion_tokens or 0)
            old_spend, old_tokens = weight.get(row.user_email, (Decimal("0"), 0))
            weight[row.user_email] = (old_spend + spend, old_tokens + tokens)

        ordered = sorted(weight, key=lambda e: (-weight[e][0], -weight[e][1], e))
        width = max(3, len(str(len(ordered))))
        labels = {email: f"user-{i:0{width}d}" for i, email in enumerate(ordered, 1)}

        rows = tuple(
            replace(
                row,
                user_email=labels.get(row.user_email) if row.user_email else None,
                account_uuid=None,
                user_name=None,
            )
            for row in self.rows
        )
        return replace(
            self,
            rows=rows,
            present=self.present - {"account_uuid", "user_name"},
        )
