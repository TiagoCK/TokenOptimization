"""A synthetic Claude Enterprise month, for trying the Enterprise paths.

Two inputs come out of one set of invented people, so they agree with each
other:

* examples/sample-enterprise-analytics.json -- what `fetch --source analytics`
  would save, in the Claude Enterprise Analytics API's documented shape;
* examples/sample-enterprise-spend.csv -- the Team/Enterprise spend report a
  plan admin can download, with its documented column names.

And the two reports built from them, by the CLI itself, so the samples are
always the output of the files sitting next to them. Regenerate with:

    python -m tests.generate_sample

Nobody here is real. The organisation is built so that every check the
Enterprise paths can run has something to say:

    priya   Engineering           Claude Code / Opus 5    1h cache writes barely re-read (C1)
    marco   Engineering           Claude Code / Sonnet 5  healthy caching
    lena    Engineering, Platform Claude Code / Opus 5    long-context work (C4); in two groups
    sam     Data                  Chat / Sonnet 5         big prompts, no caching (C2)
    ana     Sales                 Chat / Haiku 4.5
    tom     Sales                 Chat / Sonnet 5
    kim     Support               Cowork / Haiku 4.5      output-heavy drafting (C5, E4)
    raj     no group              Chat / Opus 5
    eve     no group              Claude Code / Sonnet 5  a runaway day on Aug 20 (C6a)
    a deleted user, no group      Chat / Sonnet 5         left on Aug 10

Weekends run at a fifth of weekday volume. The organisation pays 85% of list
price, so billed cost is 0.85 x the list figure throughout.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
ANALYTICS = EXAMPLES / "sample-enterprise-analytics.json"
SPEND = EXAMPLES / "sample-enterprise-spend.csv"
ANALYTICS_REPORT = EXAMPLES / "sample-enterprise-report.md"
SPEND_REPORT = EXAMPLES / "sample-enterprise-spend-report.md"
PRICING = ROOT / "pricing" / "2026-10.json"

START = datetime(2026, 8, 1, tzinfo=timezone.utc)
END = datetime(2026, 9, 1, tzinfo=timezone.utc)
MONTH = [date(2026, 8, 1) + timedelta(days=d) for d in range(31)]
FETCHED = datetime(2026, 10, 5, tzinfo=timezone.utc)
GENERATED_AT = datetime(2026, 10, 5, tzinfo=timezone.utc)
DISCOUNT = Decimal("0.85")
SPIKE_DAY = date(2026, 8, 20)
DEPARTURE = date(2026, 8, 10)

GROUPS = {
    "rbac_group_engineering": "Engineering",
    "rbac_group_platform": "Platform",
    "rbac_group_data": "Data",
    "rbac_group_sales": "Sales",
    "rbac_group_support": "Support",
}

PRODUCT_NAMES = {"claude_code": "Claude Code", "chat": "Chat", "cowork": "Cowork"}
MODEL_FAMILY = {
    "claude-opus-5": "Claude Opus",
    "claude-sonnet-5": "Claude Sonnet",
    "claude-haiku-4-5": "Claude Haiku",
}


@dataclass(frozen=True)
class Person:
    user_id: str
    email: str | None
    name: str
    product: str
    model: str
    groups: tuple[str, ...]
    #: Weekday tokens: uncached, cache_read, 5m writes, 1h writes, output.
    daily: tuple[int, int, int, int, int]
    requests: int
    context_window: str = "0-200k"
    extra: dict = field(default_factory=dict)


PEOPLE = (
    Person("user_01", "priya@example.com", "Priya", "claude_code", "claude-opus-5",
           ("rbac_group_engineering",), (1_200_000, 1_500_000, 0, 2_400_000, 350_000), 220),
    Person("user_02", "marco@example.com", "Marco", "claude_code", "claude-sonnet-5",
           ("rbac_group_engineering",), (900_000, 14_000_000, 1_000_000, 0, 420_000), 260),
    Person("user_03", "lena@example.com", "Lena", "claude_code", "claude-opus-5",
           ("rbac_group_engineering", "rbac_group_platform"),
           (3_000_000, 22_000_000, 900_000, 0, 300_000), 140, context_window="200k-1M"),
    Person("user_04", "sam@example.com", "Sam", "chat", "claude-sonnet-5",
           ("rbac_group_data",), (6_500_000, 0, 0, 0, 280_000), 90),
    Person("user_05", "ana@example.com", "Ana", "chat", "claude-haiku-4-5",
           ("rbac_group_sales",), (700_000, 300_000, 50_000, 0, 180_000), 120),
    Person("user_06", "tom@example.com", "Tom", "chat", "claude-sonnet-5",
           ("rbac_group_sales",), (1_100_000, 900_000, 120_000, 0, 260_000), 110),
    Person("user_07", "kim@example.com", "Kim", "cowork", "claude-haiku-4-5",
           ("rbac_group_support",), (350_000, 500_000, 40_000, 0, 950_000), 300),
    Person("user_08", "raj@example.com", "Raj", "chat", "claude-opus-5",
           (), (1_400_000, 1_100_000, 200_000, 0, 240_000), 70),
    Person("user_09", "eve@example.com", "Eve", "claude_code", "claude-sonnet-5",
           (), (600_000, 4_500_000, 300_000, 0, 220_000), 150),
    Person("user_10", None, "Deleted User", "chat", "claude-sonnet-5",
           (), (250_000, 0, 0, 0, 60_000), 30),
)


def _scale(person: Person, day: date) -> Decimal:
    if person.email is None and day > DEPARTURE:
        return Decimal("0")
    factor = Decimal("1") if day.weekday() < 5 else Decimal("0.2")
    if person.user_id == "user_09" and day == SPIKE_DAY:
        factor = Decimal("80")
    return factor


def _tokens(person: Person, day: date) -> tuple[int, ...]:
    scale = _scale(person, day)
    return tuple(int(Decimal(t) * scale) for t in person.daily) + (
        int(Decimal(person.requests) * scale),
    )


def _list_usd(person: Person, tokens: tuple[int, ...], pricing) -> Decimal:
    from claude_spend_audit.frame import Segment, TokenBuckets

    uncached, read, w5m, w1h, output, _requests = tokens
    buckets = TokenBuckets(
        uncached_input=uncached, cache_read=read, cache_write_5m=w5m,
        cache_write_1h=w1h, output=output,
    )
    segment = Segment(
        model=person.model, context_window=person.context_window,
        inference_geo="global", speed="standard",
    )
    return sum(pricing.bucket_costs(buckets, segment).values(), Decimal("0"))


def _cents(usd: Decimal) -> str:
    """Fractional cents, as the Analytics cost report writes them."""
    return f"{(usd * 100).quantize(Decimal('0.000001'))}"


def _ts(day: date) -> str:
    return f"{day.isoformat()}T00:00:00Z"


def _page(data: list) -> dict:
    return {
        "data": data,
        "has_more": False,
        "next_page": None,
        "data_refreshed_at": "2026-10-05T00:00:00Z",
        "organization_id": "org_sample_northwind",
    }


def _token_fields(tokens: tuple[int, ...]) -> dict:
    uncached, read, w5m, w1h, output, requests = tokens
    return {
        "uncached_input_tokens": uncached,
        "cache_read_input_tokens": read,
        "cache_creation": {
            "ephemeral_5m_input_tokens": w5m,
            "ephemeral_1h_input_tokens": w1h,
        },
        "output_tokens": output,
        "server_tool_use": {"web_search_requests": 0},
        "requests": requests,
    }


def _actor(person: Person) -> dict:
    return {
        "type": "user_actor",
        "user_id": person.user_id,
        "email_address": person.email,
        "name": person.name,
        "deleted": person.email is None,
    }


def _add(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(x + y for x, y in zip(a, b))


# --------------------------------------------------------------------------
# The two inputs
# --------------------------------------------------------------------------


def build_analytics() -> str:
    from claude_spend_audit.cli import DEFAULT_ANALYTICS_GROUP_BY
    from claude_spend_audit.ingest.api_client import (
        ANALYTICS_COST_PATH,
        ANALYTICS_USAGE_PATH,
        ANALYTICS_USER_COST_PATH,
        ANALYTICS_USER_USAGE_PATH,
        analytics_envelope,
    )
    from claude_spend_audit.pricing import PricingTable

    pricing = PricingTable.load(PRICING)
    usage_buckets, total_buckets, cost_buckets, group_buckets = [], [], [], []
    month_tokens = {p.user_id: (0,) * 6 for p in PEOPLE}
    month_usd = {p.user_id: Decimal("0") for p in PEOPLE}

    for day in MONTH:
        grouped: dict[tuple, tuple[int, ...]] = {}
        day_total = (0,) * 6
        day_usd = Decimal("0")
        by_group = {g: Decimal("0") for g in GROUPS}
        unassigned = Decimal("0")
        for person in PEOPLE:
            tokens = _tokens(person, day)
            if not any(tokens):
                continue
            usd = _list_usd(person, tokens, pricing)
            key = (person.model, person.product, person.context_window)
            grouped[key] = _add(grouped.get(key, (0,) * 6), tokens)
            day_total = _add(day_total, tokens)
            day_usd += usd
            month_tokens[person.user_id] = _add(month_tokens[person.user_id], tokens)
            month_usd[person.user_id] += usd
            for group in person.groups:
                by_group[group] += usd
            if not person.groups:
                unassigned += usd

        usage_buckets.append({
            "starting_at": _ts(day),
            "ending_at": _ts(day + timedelta(days=1)),
            "results": [
                {"model": m, "product": p, "context_window": cw, "inference_geo": "global",
                 "speed": "standard", **_token_fields(t)}
                for (m, p, cw), t in sorted(grouped.items())
            ],
        })
        total_buckets.append({
            "starting_at": _ts(day), "ending_at": _ts(day + timedelta(days=1)),
            "results": [_token_fields(day_total)],
        })
        cost_buckets.append({
            "starting_at": _ts(day), "ending_at": _ts(day + timedelta(days=1)),
            "results": [{"amount": _cents(day_usd * DISCOUNT), "list_amount": _cents(day_usd),
                         "currency": "USD"}],
        })
        group_rows = [
            {"rbac_group_id": g, "amount": _cents(usd * DISCOUNT), "list_amount": _cents(usd),
             "currency": "USD"}
            for g, usd in by_group.items() if usd
        ]
        group_rows.append({"rbac_group_id": None, "amount": _cents(unassigned * DISCOUNT),
                           "list_amount": _cents(unassigned), "currency": "USD"})
        group_buckets.append({
            "starting_at": _ts(day), "ending_at": _ts(day + timedelta(days=1)),
            "results": group_rows,
        })

    user_usage = [
        {"actor": _actor(p), "product": p.product, "model": p.model,
         **_token_fields(month_tokens[p.user_id]),
         "total_tokens": sum(month_tokens[p.user_id][:5])}
        for p in PEOPLE
    ]
    user_cost = [
        {"actor": _actor(p), "product": p.product, "model": p.model,
         "amount": _cents(month_usd[p.user_id] * DISCOUNT),
         "list_amount": _cents(month_usd[p.user_id]), "currency": "USD"}
        for p in PEOPLE
    ]

    group_by = list(DEFAULT_ANALYTICS_GROUP_BY)
    series = {
        "usage": {"endpoint": ANALYTICS_USAGE_PATH, "bucket_width": "1d",
                  "group_by": group_by, "pages": [_page(usage_buckets)]},
        "usage_total": {"endpoint": ANALYTICS_USAGE_PATH, "bucket_width": "1d",
                        "group_by": [], "pages": [_page(total_buckets)]},
        "cost_total": {"endpoint": ANALYTICS_COST_PATH, "bucket_width": "1d",
                       "group_by": [], "pages": [_page(cost_buckets)]},
        "groups_cost": {"endpoint": ANALYTICS_COST_PATH, "bucket_width": "1d",
                        "group_by": ["rbac_group_id"], "pages": [_page(group_buckets)]},
        "user_usage": {"endpoint": ANALYTICS_USER_USAGE_PATH, "group_by": ["product", "model"],
                       "pages": [_page(user_usage)]},
        "user_cost": {"endpoint": ANALYTICS_USER_COST_PATH, "group_by": ["product", "model"],
                      "pages": [_page(user_cost)]},
    }
    from claude_spend_audit import __version__

    envelope = analytics_envelope(
        starting_at=START, ending_at=END, series=series, version=__version__,
        fetched_at=FETCHED,
    )
    return json.dumps(envelope, indent=2) + "\n"


def build_spend_csv() -> str:
    """The same month as a downloaded spend report: per user, per product and
    model, summed over the range, in dollars. Prompt tokens include cache reads
    and writes."""
    from claude_spend_audit.pricing import PricingTable

    pricing = PricingTable.load(PRICING)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([
        "user_email", "account_uuid", "product", "model", "model_family", "total_requests",
        "total_prompt_tokens", "total_completion_tokens", "total_net_spend_usd",
        "total_gross_spend_usd",
    ])
    for i, person in enumerate(PEOPLE, start=1):
        tokens = (0,) * 6
        usd = Decimal("0")
        for day in MONTH:
            day_tokens = _tokens(person, day)
            tokens = _add(tokens, day_tokens)
            if any(day_tokens):
                usd += _list_usd(person, day_tokens, pricing)
        uncached, read, w5m, w1h, output, requests = tokens
        writer.writerow([
            person.email or "",
            f"6b1f2c1e-0000-4a3e-9f00-{i:012d}",
            PRODUCT_NAMES[person.product],
            person.model,
            MODEL_FAMILY[person.model],
            requests,
            uncached + read + w5m + w1h,
            output,
            f"{(usd * DISCOUNT).quantize(Decimal('0.01'))}",
            f"{usd.quantize(Decimal('0.01'))}",
        ])
    return out.getvalue()


# --------------------------------------------------------------------------
# The reports, made by the CLI itself
# --------------------------------------------------------------------------

ANALYTICS_PREAMBLE = """<!-- Generated by: python -m tests.generate_sample -->
> **This is a sample.** It comes from an invented Claude Enterprise month in
> [`sample-enterprise-analytics.json`](sample-enterprise-analytics.json) -- a
> file in the shape `fetch --source analytics` saves -- not from anyone's real
> data. Run it yourself:
>
>     claude-spend-audit audit examples/sample-enterprise-analytics.json

"""

SPEND_PREAMBLE = """<!-- Generated by: python -m tests.generate_sample -->
> **This is a sample.** It comes from the same invented month as
> [`sample-enterprise-report.md`](sample-enterprise-report.md), written as a
> Team/Enterprise spend report download:
> [`sample-enterprise-spend.csv`](sample-enterprise-spend.csv). Run it yourself:
>
>     claude-spend-audit audit examples/sample-enterprise-spend.csv

"""


def _render(source: Path, preamble: str) -> str:
    """Run the real CLI, with the clock fixed so the report is reproducible."""
    import contextlib
    import tempfile
    from unittest import mock

    from claude_spend_audit.cli import main
    from claude_spend_audit.render import markdown

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return GENERATED_AT

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "report.md"
        with mock.patch.object(markdown, "datetime", FixedClock), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            main(["audit", str(source), "--pricing", str(PRICING),
                  "--format", "markdown", "-o", str(target)])
        body = target.read_text(encoding="utf-8")
    # Paths differ per machine and per OS; the committed sample must not.
    for path in (source, PRICING):
        body = body.replace(str(path), path.relative_to(ROOT).as_posix())
    return preamble + body


def build_analytics_report() -> str:
    return _render(ANALYTICS, ANALYTICS_PREAMBLE)


def build_spend_report() -> str:
    return _render(SPEND, SPEND_PREAMBLE)
