"""Normalize Usage & Cost API JSON responses into a Frame.

The HTTP client is M4. This module is the normalizer alone, and it exists now
for two reasons:

* it is the one ingest path whose field names are fully documented, so test
  fixtures written against it are grounded in the published schema rather than
  in a guess;
* it lets tests assert that CSV and API ingest produce an identical Frame,
  which is what "checks never know which path the data came from" requires.

Field names follow the response schema exactly. Nothing here is inferred.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .. import gaps as gapmod
from ..frame import (
    DIMENSIONS,
    CostRow,
    Frame,
    Segment,
    SourceInfo,
    TokenBuckets,
    UsageRow,
)
from .schema import infer_bucket_width, parse_datetime


def load_usage_json(
    payload: Mapping[str, Any] | Sequence[Mapping[str, Any]] | str | Path,
    *,
    group_by: Iterable[str] | None = None,
    source: str = "<api>",
) -> Frame:
    """Normalize one or more usage_report/messages responses.

    ``group_by`` must be the dimensions actually requested. The response cannot
    tell us: an unrequested dimension and a genuinely-null one both come back as
    ``null``. Passing it here is what keeps Frame.grouped_by honest.
    """
    pages, source = _pages(payload, source)
    rows: list[UsageRow] = []

    for page in pages:
        for bucket in page.get("data", ()):
            start = parse_datetime(bucket["starting_at"])
            end = parse_datetime(bucket["ending_at"])
            for result in bucket.get("results", ()):
                rows.append(
                    UsageRow(
                        start=start,
                        end=end,
                        segment=Segment(
                            **{d: result.get(d) for d in DIMENSIONS}
                        ),
                        tokens=_tokens(result),
                        web_search_requests=int(
                            (result.get("server_tool_use") or {}).get(
                                "web_search_requests", 0
                            )
                            or 0
                        ),
                    )
                )

    if group_by is None:
        # Infer conservatively: a dimension counts as grouped only if some row
        # carries a non-null value for it. A dimension that is null everywhere
        # is indistinguishable from one that was never requested, and the safe
        # reading is "not grouped" -- that suppresses a C7 finding rather than
        # inventing one.
        grouped = frozenset(
            d
            for d in DIMENSIONS
            if any(getattr(r.segment, d) is not None for r in rows)
        )
    else:
        grouped = frozenset(group_by) & frozenset(DIMENSIONS)

    frame = Frame(
        usage=tuple(rows),
        grouped_by=grouped,
        bucket_width=infer_bucket_width([r.start for r in rows]),
        sources=(SourceInfo(path=source, ingest_path="api"),),
    )
    return frame.with_gaps(_usage_gaps(frame))


def load_cost_json(
    payload: Mapping[str, Any] | Sequence[Mapping[str, Any]] | str | Path,
    *,
    source: str = "<api>",
) -> Frame:
    """Normalize one or more cost_report responses."""
    pages, source = _pages(payload, source)
    rows: list[CostRow] = []
    saw_code_execution = False
    saw_session_usage = False

    for page in pages:
        for bucket in page.get("data", ()):
            start = parse_datetime(bucket["starting_at"])
            end = parse_datetime(bucket["ending_at"])
            for result in bucket.get("results", ()):
                cost_type = result.get("cost_type")
                saw_code_execution |= cost_type == "code_execution"
                saw_session_usage |= cost_type == "session_usage"
                currency = require_usd(result.get("currency"), "cost_report")
                rows.append(
                    CostRow(
                        start=start,
                        end=end,
                        # amount is a decimal string in the lowest currency
                        # unit: "123.45" USD is $1.23.
                        amount_usd=cents(result.get("amount"), "cost_report amount"),
                        currency=currency,
                        cost_type=cost_type,
                        description=result.get("description"),
                        token_type=result.get("token_type"),
                        workspace_id=result.get("workspace_id"),
                        model=result.get("model"),
                        service_tier=result.get("service_tier"),
                        context_window=result.get("context_window"),
                        inference_geo=result.get("inference_geo"),
                        # Only the Enterprise Analytics cost report carries
                        # these two; on the Usage & Cost API they stay None.
                        product=result.get("product"),
                        list_amount_usd=(
                            cents(result["list_amount"], "cost_report list_amount")
                            if result.get("list_amount") is not None
                            else None
                        ),
                    )
                )

    frame = Frame(
        cost=tuple(rows),
        sources=(SourceInfo(path=source, ingest_path="api"),),
    )
    found = []
    if saw_code_execution:
        found.append(gapmod.code_execution_cost_only())
    if saw_session_usage:
        found.append(gapmod.session_usage_cost_only())
    return frame.with_gaps(found)


class ResponseShapeError(ValueError):
    """A saved API response holds a value this tool will not guess at."""


def cents(value: object, where: str) -> Decimal:
    """An amount in the lowest currency unit, as dollars.

    A missing or unreadable amount is an error, never $0: a cost row that
    silently counted as nothing would understate the bill with no sign.
    """
    if value is None:
        raise ResponseShapeError(f"a {where} is null; the response is incomplete")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ResponseShapeError(f"a {where} reads {value!r}, not a number") from exc
    if not amount.is_finite():
        raise ResponseShapeError(f"a {where} reads {value!r}, not a finite number")
    return amount / Decimal("100")


def require_usd(currency: object, where: str) -> str:
    """The currency, which must be USD (or absent, which the API means as USD)."""
    code = str(currency or "USD")
    if code.strip().upper() != "USD":
        raise ResponseShapeError(
            f"the {where} reports amounts in {code!r}. Every figure this tool "
            "produces is in US dollars, and it does not convert currencies."
        )
    return code


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _pages(
    payload: Mapping[str, Any] | Sequence[Mapping[str, Any]] | str | Path,
    source: str,
) -> tuple[list[Mapping[str, Any]], str]:
    """Accept a single response, a list of pages, or a path to either."""
    if isinstance(payload, (str, Path)):
        p = Path(payload)
        source = str(p)
        payload = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(payload, Mapping):
        return [payload], source
    return list(payload), source


def _tokens(result: Mapping[str, Any]) -> TokenBuckets:
    """Flatten the response's nested cache_creation into the five buckets."""
    creation = result.get("cache_creation") or {}
    return TokenBuckets(
        uncached_input=int(result.get("uncached_input_tokens", 0) or 0),
        cache_read=int(result.get("cache_read_input_tokens", 0) or 0),
        cache_write_5m=int(creation.get("ephemeral_5m_input_tokens", 0) or 0),
        cache_write_1h=int(creation.get("ephemeral_1h_input_tokens", 0) or 0),
        output=int(result.get("output_tokens", 0) or 0),
    )


def _usage_gaps(frame: Frame) -> list:
    """Gaps about the data itself.

    Which checks this shape of export can support is the check registry's
    business -- those gaps come from each check's declared preconditions in
    checks.run_checks(), so a renamed check cannot leave a stale string here.
    """
    found = [gapmod.bedrock_not_visible(detected=False)]

    if "service_tier" in frame.grouped_by:
        for tier in frame.distinct("service_tier"):
            if tier and tier.startswith("priority"):
                found.append(gapmod.priority_tier_present(tier))

    return found
