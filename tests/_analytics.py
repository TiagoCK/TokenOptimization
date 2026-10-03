"""Synthetic Claude Enterprise Analytics API responses, in the documented shape.

Field names are from Anthropic's API reference for usage_report, cost_report,
user_usage_report and user_cost_report, read 2026-10-03. No real account has
been seen; these exist so every figure below can be checked by hand.

The month, ten days from 2026-08-01, identical every day:

    claude-opus-5   / claude_code   1,000,000 uncached   500,000 1h writes
                                      200,000 output
    claude-sonnet-5 / chat            500,000 uncached   100,000 output

    opus    1.0M x $5 + 0.5M x $10 + 0.2M x $25  = $5 + $5 + $5 = $15.00
    sonnet  0.5M x $2 + 0.1M x $10               = $1 + $1      =  $2.00
                                                   per day       $17.00
                                                   ten days     $170.00

Billed $16.00 a day against a $20.00 list price: $160.00 of $200.00, 80.0%.

Spend by RBAC group, per day: rbac_group_eng $12.00, rbac_group_sales $3.00,
users in no group $4.00. Unassigned: $40.00 of $160.00, 25%. The named rows
plus the null row come to $19.00 a day, more than the $16.00 billed --
deliberately, because a user in two groups counts in both.

Per user (two windows of the user endpoints, so summing across them is
exercised):

    a@example.com   claude_code / claude-opus-5    $160.00 net, $200.00 list
    deleted user    chat / claude-sonnet-5          $10.00 net,  $12.50 list
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from claude_spend_audit.ingest.api_client import analytics_envelope

START = datetime(2026, 8, 1, tzinfo=timezone.utc)
END = START + timedelta(days=10)
FETCHED = datetime(2026, 10, 3, tzinfo=timezone.utc)
REFRESHED = "2026-10-03T00:00:00Z"
GROUP_BY = ["model", "product", "context_window", "inference_geo", "speed"]


def _ts(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def usage_row(model, product, *, uncached=0, cache_read=0, w5m=0, w1h=0, output=0, **dims):
    row = {
        "model": model,
        "product": product,
        "context_window": "0-200k",
        "inference_geo": "global",
        "speed": "standard",
        "uncached_input_tokens": uncached,
        "cache_read_input_tokens": cache_read,
        "cache_creation": {
            "ephemeral_5m_input_tokens": w5m,
            "ephemeral_1h_input_tokens": w1h,
        },
        "output_tokens": output,
        "server_tool_use": {"web_search_requests": 0},
        "requests": 10,
    }
    row.update(dims)
    return row


OPUS = dict(uncached=1_000_000, w1h=500_000, output=200_000)
SONNET = dict(uncached=500_000, output=100_000)


def page(data: list, refreshed: str | None = REFRESHED) -> dict[str, Any]:
    return {
        "data": data,
        "has_more": False,
        "next_page": None,
        "data_refreshed_at": refreshed,
        "organization_id": "org_test",
    }


def _days(rows_for_day) -> list[dict]:
    buckets = []
    for d in range(10):
        start = START + timedelta(days=d)
        buckets.append(
            {
                "starting_at": _ts(start),
                "ending_at": _ts(start + timedelta(days=1)),
                "results": rows_for_day(d),
            }
        )
    return buckets


def grouped_usage(*, drop_sonnet_on: tuple[int, ...] = ()) -> list[dict]:
    """The grouped series. ``drop_sonnet_on`` simulates the 100-group cap."""

    def rows(day):
        out = [usage_row("claude-opus-5", "claude_code", **OPUS)]
        if day not in drop_sonnet_on:
            out.append(usage_row("claude-sonnet-5", "chat", **SONNET))
        return out

    return _days(rows)


def total_usage() -> list[dict]:
    """The ungrouped series: one combined row per bucket, dimensions null."""

    def rows(_day):
        return [
            {
                "uncached_input_tokens": 1_500_000,
                "cache_read_input_tokens": 0,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 500_000,
                },
                "output_tokens": 300_000,
                "server_tool_use": {"web_search_requests": 0},
                "requests": 20,
            }
        ]

    return _days(rows)


def total_cost() -> list[dict]:
    return _days(
        lambda _day: [{"amount": "1600.000000", "list_amount": "2000.000000", "currency": "USD"}]
    )


def _user_usage(user_id, email, name, product, model, uncached, w1h, output, requests):
    return {
        "actor": {
            "type": "user_actor",
            "user_id": user_id,
            "email_address": email,
            "name": name,
            "deleted": email is None,
        },
        "product": product,
        "model": model,
        "uncached_input_tokens": uncached,
        "cache_read_input_tokens": 0,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": w1h},
        "output_tokens": output,
        "total_tokens": uncached + w1h + output,
        "requests": requests,
        "server_tool_use": {"web_search_requests": 0},
    }


def _user_cost(user_id, email, name, product, model, amount, list_amount):
    return {
        "actor": {
            "type": "user_actor",
            "user_id": user_id,
            "email_address": email,
            "name": name,
            "deleted": email is None,
        },
        "product": product,
        "model": model,
        "amount": amount,
        "list_amount": list_amount,
        "currency": "USD",
    }


def user_usage_pages() -> list[dict]:
    """Two windows' worth; a@example.com appears in both and must be summed."""
    alice = ("user_01", "a@example.com", "Alice", "claude_code", "claude-opus-5")
    gone = ("user_02", None, "Deleted User", "chat", "claude-sonnet-5")
    return [
        page([_user_usage(*alice, 5_000_000, 2_500_000, 1_000_000, 50)]),
        page(
            [
                _user_usage(*alice, 5_000_000, 2_500_000, 1_000_000, 50),
                _user_usage(*gone, 5_000_000, 0, 1_000_000, 50),
            ]
        ),
    ]


def user_cost_pages() -> list[dict]:
    alice = ("user_01", "a@example.com", "Alice", "claude_code", "claude-opus-5")
    gone = ("user_02", None, "Deleted User", "chat", "claude-sonnet-5")
    return [
        page([_user_cost(*alice, "8000.000000", "10000.000000")]),
        page(
            [
                _user_cost(*alice, "8000.000000", "10000.000000"),
                _user_cost(*gone, "1000.000000", "1250.000000"),
            ]
        ),
    ]


def groups_cost_pages(*, capped_on: tuple[int, ...] = (), null_cents: str = "400.000000") -> list[dict]:
    """Cost grouped by rbac_group_id. ``capped_on`` days carry 100 named rows
    and no null row, as the API's group cap would leave them."""

    def rows(day):
        if day in capped_on:
            return [
                {"rbac_group_id": f"rbac_group_{i:03d}", "amount": "10.000000",
                 "list_amount": "12.500000", "currency": "USD"}
                for i in range(100)
            ]
        return [
            {"rbac_group_id": "rbac_group_eng", "amount": "1200.000000", "list_amount": "1500.000000", "currency": "USD"},
            {"rbac_group_id": "rbac_group_sales", "amount": "300.000000", "list_amount": "375.000000", "currency": "USD"},
            {"rbac_group_id": None, "amount": null_cents, "list_amount": "500.000000", "currency": "USD"},
        ]

    return [page(_days(rows))]


def fetched(
    *,
    drop_sonnet_on: tuple[int, ...] = (),
    refreshed: str | None = REFRESHED,
    fetched_at: datetime = FETCHED,
    users: bool = True,
    groups: bool = True,
    groups_capped_on: tuple[int, ...] = (),
    group_by: list[str] | None = None,
) -> dict[str, Any]:
    """A whole saved fetch, as `fetch --source analytics` would write it."""
    series: dict[str, dict] = {
        "usage": {
            "endpoint": "/v1/organizations/analytics/usage_report",
            "bucket_width": "1d",
            "group_by": GROUP_BY if group_by is None else group_by,
            "pages": [page(grouped_usage(drop_sonnet_on=drop_sonnet_on), refreshed)],
        },
        "usage_total": {
            "endpoint": "/v1/organizations/analytics/usage_report",
            "bucket_width": "1d",
            "group_by": [],
            "pages": [page(total_usage(), refreshed)],
        },
        "cost_total": {
            "endpoint": "/v1/organizations/analytics/cost_report",
            "bucket_width": "1d",
            "group_by": [],
            "pages": [page(total_cost(), refreshed)],
        },
    }
    if groups:
        series["groups_cost"] = {
            "endpoint": "/v1/organizations/analytics/cost_report",
            "bucket_width": "1d",
            "group_by": ["rbac_group_id"],
            "pages": groups_cost_pages(capped_on=groups_capped_on),
        }
    if users:
        series["user_usage"] = {
            "endpoint": "/v1/organizations/analytics/user_usage_report",
            "group_by": ["product", "model"],
            "pages": user_usage_pages(),
        }
        series["user_cost"] = {
            "endpoint": "/v1/organizations/analytics/user_cost_report",
            "group_by": ["product", "model"],
            "pages": user_cost_pages(),
        }
    return analytics_envelope(
        starting_at=START,
        ending_at=END,
        series=series,
        version="0.1.0",
        fetched_at=fetched_at,
    )
