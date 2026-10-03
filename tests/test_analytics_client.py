"""The Claude Enterprise Analytics API client. No test touches the network.

Same rules as the Admin API client, plus the Analytics API's own: ranges of at
most 31 days, nothing before 2026-01-01 or older than 365 days, lower page
caps, and cursors that can expire mid-pagination (HTTP 410) when the data
refreshes underneath them.
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from claude_spend_audit.ingest.api_client import (
    ANALYTICS_USAGE_PATH,
    ANALYTICS_USER_USAGE_PATH,
    ApiClient,
    ApiError,
    AuthError,
    CursorExpired,
    analytics_envelope,
    is_analytics_envelope,
    is_envelope,
)

from ._transport import FakeTransport, error, no_sleep, ok

KEY = "sk-ant-api01-ANALYTICSSECRET987"
NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)
START = datetime(2026, 8, 1, tzinfo=timezone.utc)


def client(*responses, **kw):
    transport = FakeTransport(*responses)
    api = ApiClient(
        KEY,
        transport=transport,
        sleep=no_sleep,
        jitter=lambda: 0.0,
        key_kind="analytics",
        clock=lambda: NOW,
        **kw,
    )
    return api, transport


def empty(**extra):
    return {"data": [], "has_more": False, "next_page": None, "data_refreshed_at": None, **extra}


def _q(url):
    return parse_qs(urlsplit(url).query)


def test_usage_request_shape():
    api, transport = client(ok(empty()))
    api.analytics_usage(
        START, START + timedelta(days=7), group_by=["model", "product"]
    )
    url = transport.urls()[0]
    assert urlsplit(url).path == ANALYTICS_USAGE_PATH
    query = _q(url)
    assert query["bucket_width"] == ["1d"]
    assert query["limit"] == ["31"]
    assert query["group_by[]"] == ["model", "product"]
    assert transport.header("x-api-key") == KEY
    assert KEY not in url


def test_ranges_are_windowed_to_31_days_without_overlap():
    api, transport = client(ok(empty()), ok(empty()))
    api.analytics_usage(START, START + timedelta(days=40))
    starts = [_q(u)["starting_at"][0] for u in transport.urls()]
    ends = [_q(u)["ending_at"][0] for u in transport.urls()]
    assert starts == ["2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z"]
    assert ends == ["2026-09-01T00:00:00Z", "2026-09-10T00:00:00Z"]


def test_hourly_windows_follow_the_168_bucket_cap():
    """168 hourly buckets is seven days, so eight days takes two requests."""
    api, transport = client(ok(empty()), ok(empty()))
    api.analytics_usage(START, START + timedelta(days=8), bucket_width="1h")
    assert transport.calls == 2
    assert _q(transport.urls()[0])["limit"] == ["168"]


def test_limits_above_the_analytics_caps_are_refused_before_sending():
    api, transport = client()
    with pytest.raises(ApiError, match="outside 1..256"):
        api.analytics_usage(START, START + timedelta(hours=2), bucket_width="1m", limit=300)
    assert transport.calls == 0


def test_unknown_group_by_is_refused():
    api, transport = client()
    with pytest.raises(ApiError, match="workspace_id"):
        api.analytics_usage(START, START + timedelta(days=1), group_by=["workspace_id"])
    assert transport.calls == 0


def test_dates_before_2026_are_refused():
    api, transport = client()
    with pytest.raises(ApiError, match="before 2026-01-01"):
        api.analytics_usage(
            datetime(2025, 12, 1, tzinfo=timezone.utc), datetime(2026, 1, 5, tzinfo=timezone.utc)
        )
    assert transport.calls == 0


def test_dates_older_than_365_days_are_refused():
    transport = FakeTransport()
    api = ApiClient(
        KEY,
        transport=transport,
        sleep=no_sleep,
        key_kind="analytics",
        clock=lambda: datetime(2027, 9, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(ApiError, match="365 days"):
        api.analytics_usage(START, START + timedelta(days=1))
    assert transport.calls == 0


def test_an_expired_cursor_restarts_the_window_from_page_one():
    """Page 1, then 410 on page 2: restart, and return only the second pass."""
    first = empty(has_more=True, next_page="cursor_a")
    first["data"] = [{"starting_at": "2026-08-01T00:00:00Z", "ending_at": "2026-08-02T00:00:00Z", "results": []}]
    second = empty()
    second["data"] = [{"starting_at": "2026-08-02T00:00:00Z", "ending_at": "2026-08-03T00:00:00Z", "results": []}]
    api, transport = client(ok(first), error(410, "cursor expired"), ok(first), ok(second))
    pages = api.analytics_usage(START, START + timedelta(days=2))
    assert transport.calls == 4
    assert len(pages) == 2, "pages from the abandoned pass must not be kept"


def test_repeated_cursor_expiry_returns_nothing():
    api, transport = client(error(410), error(410), error(410))
    with pytest.raises(CursorExpired, match="Nothing from this window"):
        api.analytics_usage(START, START + timedelta(days=2))
    assert transport.calls == 3


def test_auth_errors_name_the_right_key():
    api, _ = client(error(403, f"bad key {KEY}"))
    with pytest.raises(AuthError) as caught:
        api.analytics_usage(START, START + timedelta(days=1))
    message = str(caught.value)
    assert "read:analytics" in message
    assert "Primary Owner" in message
    assert KEY not in message


def test_user_endpoints_take_no_bucket_width():
    api, transport = client(ok(empty()), ok(empty()))
    api.analytics_user_usage(START, START + timedelta(days=40), group_by=["product", "model"])
    assert transport.calls == 2
    url = transport.urls()[0]
    assert urlsplit(url).path == ANALYTICS_USER_USAGE_PATH
    assert "bucket_width" not in _q(url)
    assert _q(url)["limit"] == ["1000"]


def test_the_envelope_holds_no_credential_and_is_its_own_kind():
    payload = analytics_envelope(
        starting_at=START,
        ending_at=START + timedelta(days=1),
        series={"usage": {"endpoint": ANALYTICS_USAGE_PATH, "group_by": ["model"], "pages": []}},
        version="0.1.0",
    )
    assert is_analytics_envelope(payload)
    assert not is_envelope(payload)
    assert payload["series"]["usage"]["group_by"] == ["model"]
    assert KEY not in repr(payload)
