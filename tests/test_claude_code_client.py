"""The Claude Code Analytics client: one request per UTC day, every page.

The endpoint answers one day per call, so a range is a sequence of days, and
each day may run to several pages. What must never happen is a short day
returned as if complete -- so a cursor that does not move is refused.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from claude_spend_audit.ingest.api_client import (
    CLAUDE_CODE_PATH,
    ApiClient,
    ApiError,
    Credential,
    ProtocolError,
)

from ._transport import FakeTransport, error, no_sleep, ok

KEY = "sk-ant-admin01-SECRETVALUE123"
NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)


def client(transport) -> ApiClient:
    return ApiClient(Credential(KEY), transport=transport, sleep=no_sleep, clock=lambda: NOW)


def page(n: int, *, has_more=False, next_page=None) -> dict:
    return {"data": [{"date": "x"}] * n, "has_more": has_more, "next_page": next_page}


def q(request) -> dict:
    return parse_qs(urlsplit(request.url).query)


def test_one_request_per_day_with_the_date_and_largest_page():
    transport = FakeTransport(ok(page(1)), ok(page(2)), ok(page(0)))
    days = client(transport).claude_code(
        datetime(2026, 8, 3, tzinfo=timezone.utc), datetime(2026, 8, 6, tzinfo=timezone.utc)
    )
    assert [d["date"] for d in days] == ["2026-08-03", "2026-08-04", "2026-08-05"]
    assert [q(r)["starting_at"] for r in transport.requests] == [
        ["2026-08-03"], ["2026-08-04"], ["2026-08-05"]
    ]
    assert all(q(r)["limit"] == ["1000"] for r in transport.requests)
    assert all(urlsplit(r.url).path == CLAUDE_CODE_PATH for r in transport.requests)


def test_a_day_is_followed_to_its_last_page():
    transport = FakeTransport(ok(page(1000, has_more=True, next_page="p2")), ok(page(5)))
    days = client(transport).claude_code(
        datetime(2026, 8, 3, tzinfo=timezone.utc), datetime(2026, 8, 4, tzinfo=timezone.utc)
    )
    assert len(days[0]["pages"]) == 2
    assert q(transport.last)["page"] == ["p2"]


def test_a_cursor_that_does_not_move_is_refused():
    transport = FakeTransport(
        ok(page(1, has_more=True, next_page="p2")), ok(page(1, has_more=True, next_page="p2"))
    )
    with pytest.raises(ProtocolError, match="partial day"):
        client(transport).claude_code(
            datetime(2026, 8, 3, tzinfo=timezone.utc), datetime(2026, 8, 4, tzinfo=timezone.utc)
        )


def test_rate_limits_are_retried():
    transport = FakeTransport(error(429, "slow down", **{"retry-after": "1"}), ok(page(1)))
    days = client(transport).claude_code(
        datetime(2026, 8, 3, tzinfo=timezone.utc), datetime(2026, 8, 4, tzinfo=timezone.utc)
    )
    assert transport.calls == 2 and len(days) == 1


def test_the_key_never_reaches_a_url():
    transport = FakeTransport(ok(page(1)))
    client(transport).claude_code(
        datetime(2026, 8, 3, tzinfo=timezone.utc), datetime(2026, 8, 4, tzinfo=timezone.utc)
    )
    assert KEY not in transport.last.url
    assert transport.header("x-api-key") == KEY


def test_progress_is_reported_per_day():
    seen = []
    transport = FakeTransport(ok(page(1)), ok(page(1)))
    client(transport).claude_code(
        datetime(2026, 8, 3, tzinfo=timezone.utc),
        datetime(2026, 8, 5, tzinfo=timezone.utc),
        progress=lambda day, pages: seen.append((day, pages)),
    )
    assert seen == [("2026-08-03", 1), ("2026-08-04", 1)]


def test_an_empty_or_future_range_is_refused():
    with pytest.raises(ApiError, match="empty"):
        client(FakeTransport()).claude_code(
            datetime(2026, 8, 3, tzinfo=timezone.utc), datetime(2026, 8, 3, tzinfo=timezone.utc)
        )
    with pytest.raises(ApiError, match="future"):
        client(FakeTransport()).claude_code(
            datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2026, 11, 2, tzinfo=timezone.utc)
        )


def test_days_after_today_are_not_requested():
    """A range running past today stops at today: there is nothing later."""
    transport = FakeTransport(ok(page(1)), ok(page(1)))
    days = client(transport).claude_code(
        datetime(2026, 10, 2, tzinfo=timezone.utc), datetime(2026, 10, 9, tzinfo=timezone.utc)
    )
    assert [d["date"] for d in days] == ["2026-10-02", "2026-10-03"]
