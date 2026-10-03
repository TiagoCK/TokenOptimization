"""Normalize a saved Claude Enterprise Analytics API fetch.

The Analytics API's usage and cost reports use the Usage & Cost API's own wire
shape -- time buckets of ``results`` carrying the same five token fields -- so
``api_json`` reads them unchanged. This module adds what is specific to the
source:

* a reconciliation of grouped buckets against an ungrouped total, because the
  API returns at most the top 100 groups per bucket and no remainder row;
* the freshness watermark (``data_refreshed_at``) and the 30-day revision
  window, raised as gaps;
* a ``SpendReport`` built from the per-user endpoints, so the descriptive
  E-checks run on documented fields rather than a guessed CSV mapping.

Every field name here is from Anthropic's published API reference.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .. import gaps as gapmod
from ..enterprise.report import SpendReport, SpendRow
from ..frame import Frame, SourceInfo, merge
from ..gaps import GapCode
from .api_json import load_cost_json, load_usage_json
from .schema import parse_datetime

#: Named-group rows overlap -- a user in two groups counts in both -- so a
#: Frame built from them would count the same tokens twice.
_OVERLAPPING = frozenset({"rbac_group_id"})

#: How long the API keeps revising a day's figures.
_REVISION_WINDOW = timedelta(days=30)


class AnalyticsFileError(ValueError):
    """A saved analytics fetch is missing something it must contain."""


def load_analytics(
    envelope: Mapping[str, Any], *, source: str = "<analytics>"
) -> tuple[Frame, SpendReport | None]:
    """A Frame from the bucketed series, and a SpendReport from the per-user ones."""
    series: Mapping[str, Mapping[str, Any]] = envelope.get("series") or {}
    usage = series.get("usage")
    if not usage:
        raise AnalyticsFileError(
            "this analytics fetch has no 'usage' series, so there is nothing to "
            "audit. Fetch again with `fetch --source analytics`."
        )
    group_by = tuple(usage.get("group_by") or ())
    overlapping = sorted(_OVERLAPPING & set(group_by))
    if overlapping:
        raise AnalyticsFileError(
            f"the usage series is grouped by {', '.join(overlapping)}, whose rows "
            "overlap: a user in several groups is counted in each. Totals built "
            "from it would count the same tokens more than once. Fetch again "
            "without that dimension."
        )

    info = SourceInfo(path=source, ingest_path="analytics")
    usage_frame = load_usage_json(usage.get("pages") or [], group_by=group_by, source=source)
    usage_frame = replace(
        usage_frame,
        sources=(info,),
        # api_json's Bedrock note describes the Usage API; the plan-context gap
        # below says what applies to this source instead.
        gaps=tuple(g for g in usage_frame.gaps if g.code is not GapCode.BEDROCK_NOT_VISIBLE),
    )

    frame = usage_frame
    cost = series.get("cost_total")
    if cost:
        cost_frame = load_cost_json(cost.get("pages") or [], source=source)
        frame = merge(usage_frame, replace(cost_frame, sources=(info,)))
        # merge() concatenates sources; both halves came from one file.
        frame = replace(frame, sources=(info,))

    request = envelope.get("request") or {}
    requested_end = _parse(request.get("ending_at"))
    fetched_at = _parse(envelope.get("fetched_at"))

    found = []
    total = series.get("usage_total")
    if total and group_by:
        truncated = _truncation(usage.get("pages") or [], total.get("pages") or [])
        if truncated is not None:
            found.append(truncated)

    pages = [p for s in series.values() for p in (s.get("pages") or [])]
    stamps = [p.get("data_refreshed_at") for p in pages]
    if any(stamp is None for stamp in stamps):
        found.append(gapmod.analytics_incomplete_tail(None))
    else:
        known = [_parse(stamp) for stamp in stamps if stamp]
        if known and requested_end and requested_end > min(known):
            found.append(
                gapmod.analytics_incomplete_tail(f"{min(known):%Y-%m-%d %H:%M} UTC")
            )

    requested_start = _parse(request.get("starting_at"))
    if requested_start and requested_end and fetched_at:
        recent_from = max(requested_start, fetched_at - _REVISION_WINDOW)
        if requested_end > recent_from:
            days = (requested_end - recent_from + timedelta(hours=23)).days
            found.append(gapmod.analytics_not_final(max(days, 1)))

    billed, listed = frame.billed_total_usd, frame.list_total_usd
    if billed is not None and listed:
        found.append(gapmod.list_price_basis(f"{billed / listed:.1%}"))
    found.append(gapmod.analytics_plan_context())

    report = _spend_report(
        series.get("user_usage"),
        series.get("user_cost"),
        info,
        start=requested_start,
        end=requested_end,
    )
    return frame.with_gaps(found), report


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _parse(value: object) -> datetime | None:
    return parse_datetime(str(value)) if value else None


def _tokens(result: Mapping[str, Any]) -> int:
    creation = result.get("cache_creation") or {}
    return sum(
        int(value or 0)
        for value in (
            result.get("uncached_input_tokens"),
            result.get("cache_read_input_tokens"),
            creation.get("ephemeral_5m_input_tokens"),
            creation.get("ephemeral_1h_input_tokens"),
            result.get("output_tokens"),
        )
    )


def _by_bucket(pages: Iterable[Mapping[str, Any]]) -> dict[datetime, int]:
    totals: dict[datetime, int] = {}
    for page in pages:
        for bucket in page.get("data", ()):
            start = parse_datetime(bucket["starting_at"])
            totals[start] = totals.get(start, 0) + sum(
                _tokens(r) for r in bucket.get("results", ())
            )
    return totals


def _truncation(grouped_pages, total_pages):
    """The truncation gap, or None if every grouped bucket adds up."""
    grouped = _by_bucket(grouped_pages)
    total = _by_bucket(total_pages)
    missing = 0
    short = 0
    for start, expected in total.items():
        shortfall = expected - grouped.get(start, 0)
        if shortfall > 0:
            missing += shortfall
            short += 1
    if not missing:
        return None
    whole = sum(total.values())
    share = f"{Decimal(missing) / Decimal(whole):.1%}" if whole else "all"
    return gapmod.analytics_groups_truncated(missing, share, short)


def _label(actor: Mapping[str, Any]) -> str:
    """Email, else name with id (deleted users all share a name), else id."""
    user_id = str(actor.get("user_id") or "unknown user")
    if actor.get("email_address"):
        return str(actor["email_address"])
    if actor.get("name"):
        return f"{actor['name']} ({user_id})"
    return user_id


def _cents(value: object) -> Decimal:
    return Decimal(str(value)) / Decimal("100") if value is not None else Decimal("0")


def _spend_report(
    user_usage: Mapping[str, Any] | None,
    user_cost: Mapping[str, Any] | None,
    info: SourceInfo,
    *,
    start: datetime | None,
    end: datetime | None,
) -> SpendReport | None:
    """Per-user rows, joined on (user, product, model) and summed across windows."""
    if not user_usage and not user_cost:
        return None

    acc: dict[tuple[str, str | None, str | None], dict[str, Any]] = {}

    def entry(row: Mapping[str, Any]) -> dict[str, Any]:
        actor = row.get("actor") or {}
        key = (str(actor.get("user_id")), row.get("product"), row.get("model"))
        if key not in acc:
            acc[key] = {
                "user_email": _label(actor),
                "product": row.get("product"),
                "model": row.get("model"),
                "requests": 0,
                "uncached": 0,
                "cache_read": 0,
                "cache_write": 0,
                "output": 0,
                "web_search": 0,
                "net": Decimal("0"),
                "gross": Decimal("0"),
            }
        return acc[key]

    for page in (user_usage or {}).get("pages") or []:
        for row in page.get("data", ()):
            e = entry(row)
            creation = row.get("cache_creation") or {}
            e["requests"] += int(row.get("requests") or 0)
            e["uncached"] += int(row.get("uncached_input_tokens") or 0)
            e["cache_read"] += int(row.get("cache_read_input_tokens") or 0)
            e["cache_write"] += int(creation.get("ephemeral_5m_input_tokens") or 0) + int(
                creation.get("ephemeral_1h_input_tokens") or 0
            )
            e["output"] += int(row.get("output_tokens") or 0)
            e["web_search"] += int((row.get("server_tool_use") or {}).get("web_search_requests") or 0)

    for page in (user_cost or {}).get("pages") or []:
        for row in page.get("data", ()):
            e = entry(row)
            e["net"] += _cents(row.get("amount"))
            e["gross"] += _cents(row.get("list_amount"))

    present = {"user_email"}
    grouped: set[str] = set()
    for part in (user_usage, user_cost):
        if part:
            grouped |= set(part.get("group_by") or ())
    present |= {name for name in ("product", "model") if name in grouped}
    if user_usage:
        present |= {
            "requests",
            "prompt_tokens",
            "completion_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "web_search_count",
        }
    if user_cost:
        present |= {"net_spend_usd", "gross_spend_usd"}

    def value(name: str, amount):
        return amount if name in present else None

    rows = tuple(
        SpendRow(
            user_email=e["user_email"],
            product=value("product", e["product"]),
            model=value("model", e["model"]),
            requests=value("requests", e["requests"]),
            prompt_tokens=value(
                "prompt_tokens", e["uncached"] + e["cache_read"] + e["cache_write"]
            ),
            completion_tokens=value("completion_tokens", e["output"]),
            cache_read_tokens=value("cache_read_tokens", e["cache_read"]),
            cache_write_tokens=value("cache_write_tokens", e["cache_write"]),
            web_search_count=value("web_search_count", e["web_search"]),
            net_spend_usd=value("net_spend_usd", e["net"]),
            gross_spend_usd=value("gross_spend_usd", e["gross"]),
        )
        for e in acc.values()
    )
    return SpendReport(
        rows=rows,
        present=frozenset(present),
        period_start=start,
        period_end=end,
        sources=(info,),
        prompt_includes_cache=True if user_usage else None,
    )
