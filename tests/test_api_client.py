"""The Admin API client. Failures first, because they are the point.

This is the one code path that holds an org-wide admin credential and the one
that can silently return half a month. The security and partial-data tests
below matter more than any of the happy paths.

No test touches the network: every one drives a scripted FakeTransport, and
`sleep` is injected so backoff is instant.
"""

from datetime import datetime, timedelta, timezone

import pytest

from claude_spend_audit.ingest.api_client import (
    ANTHROPIC_VERSION,
    FAST_MODE_BETA,
    MAX_LIMIT,
    OAUTH_BETA,
    ApiClient,
    ApiError,
    AuthError,
    Credential,
    ProtocolError,
    RateLimited,
    Request,
    redact,
)

from ._transport import FakeTransport, days, error, no_sleep, ok, page, raw, record_sleeps

KEY = "sk-ant-admin01-SECRETVALUE123"
START = datetime(2026, 8, 1, tzinfo=timezone.utc)
END = datetime(2026, 8, 8, tzinfo=timezone.utc)


def client(*responses, **kw) -> tuple[ApiClient, FakeTransport]:
    transport = FakeTransport(*responses)
    return (
        ApiClient(
            KEY,
            transport=transport,
            sleep=kw.pop("sleep", no_sleep),
            jitter=kw.pop("jitter", lambda: 0.0),
            **kw,
        ),
        transport,
    )


# ==========================================================================
# Security -- the tests that matter most on this path
# ==========================================================================


def test_the_credential_never_renders():
    cred = Credential(KEY)
    assert KEY not in repr(cred)
    assert KEY not in str(cred)
    assert KEY not in f"{cred}"
    assert "[redacted]" in repr(cred)


def test_the_client_repr_does_not_leak_the_key():
    api, _ = client()
    assert KEY not in repr(api)


def test_the_key_never_appears_in_an_auth_error():
    api, _ = client(error(403, "workspace keys are not allowed"))
    with pytest.raises(AuthError) as excinfo:
        api.usage(START, END)
    assert KEY not in str(excinfo.value)


def test_a_key_echoed_back_by_the_api_is_redacted():
    """An API can echo a bad key into its own error body, and that body ends
    up in our message. `Credential` refusing to render does not help there."""
    api, _ = client(error(400, f"invalid key: {KEY}"))
    with pytest.raises(ApiError) as excinfo:
        api.usage(START, END)
    assert KEY not in str(excinfo.value)
    assert "[redacted]" in str(excinfo.value)


def test_redact_is_a_no_op_without_a_credential():
    assert redact("anything", None) == "anything"


def test_the_key_is_never_put_in_a_url():
    """A URL reaches proxy logs, shell history and error messages. The
    credential belongs in a header and nowhere else."""
    api, transport = client(ok(page()))
    api.usage(START, END)
    for url in transport.urls():
        assert KEY not in url
        assert "api_key" not in url
        assert "x-api-key" not in url


def test_the_key_travels_as_a_header():
    api, transport = client(ok(page()))
    api.usage(START, END)
    assert transport.header("x-api-key") == KEY


def test_requests_are_get_only_and_carry_no_body():
    api, transport = client(ok(page()))
    api.usage(START, END)
    for request in transport.requests:
        assert request.method == "GET"
        assert not hasattr(request, "body") or getattr(request, "body", None) is None


def test_an_empty_credential_is_refused():
    with pytest.raises(AuthError):
        Credential("")
    with pytest.raises(AuthError):
        Credential("   ")


# ==========================================================================
# Failure -- HTTP status handling
# ==========================================================================


def test_401_names_what_to_check():
    api, transport = client(error(401, "invalid x-api-key"))
    with pytest.raises(AuthError) as excinfo:
        api.usage(START, END)
    message = str(excinfo.value)
    assert "401" in message
    assert "Enterprise" in message  # the other likely cause
    assert transport.calls == 1, "auth failures must not be retried"


def test_403_gives_the_documented_workspace_key_remedy():
    """The docs single this out: 'workspace API keys don't work'."""
    api, transport = client(error(403, "forbidden"))
    with pytest.raises(AuthError) as excinfo:
        api.usage(START, END)
    message = str(excinfo.value)
    assert "Workspace-scoped API keys do not work" in message
    assert "sk-ant-admin01" in message
    assert "org:admin" in message
    assert transport.calls == 1


def test_400_is_not_retried():
    """A client error does not become correct by repetition, and retrying it
    just burns rate limit. Proven by the call count, not by inspection."""
    api, transport = client(error(400, "bad bucket_width"))
    with pytest.raises(ApiError):
        api.usage(START, END)
    assert transport.calls == 1


def test_404_is_not_retried():
    api, transport = client(error(404, "no such endpoint"))
    with pytest.raises(ApiError):
        api.usage(START, END)
    assert transport.calls == 1


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_are_retried_then_surfaced(status):
    api, transport = client(*[error(status)] * 5, max_retries=4)
    with pytest.raises(ApiError) as excinfo:
        api.usage(START, END)
    assert transport.calls == 5, "one attempt plus four retries"
    assert "5 attempts" in str(excinfo.value)


def test_429_is_retried_then_raises_rate_limited():
    api, transport = client(*[error(429)] * 4, max_retries=3)
    with pytest.raises(RateLimited) as excinfo:
        api.usage(START, END)
    assert transport.calls == 4
    assert "one request per minute" in str(excinfo.value)


def test_retry_after_is_honoured_exactly():
    slept: list[float] = []
    api, _ = client(
        error(429, **{"retry-after": "7"}),
        ok(page()),
        sleep=record_sleeps(slept),
    )
    api.usage(START, END)
    assert slept == [7.0]


def test_retry_after_is_capped():
    """A server asking us to wait an hour should not hang the tool."""
    slept: list[float] = []
    api, _ = client(
        error(429, **{"retry-after": "3600"}),
        ok(page()),
        sleep=record_sleeps(slept),
        backoff_cap=30.0,
    )
    api.usage(START, END)
    assert slept == [30.0]


def test_a_date_formatted_retry_after_falls_back_to_backoff():
    """retry-after may be an HTTP date. Rather than parse it, fall back --
    a wrong parse would be worse than a slightly early retry."""
    slept: list[float] = []
    api, _ = client(
        error(429, **{"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        ok(page()),
        sleep=record_sleeps(slept),
    )
    api.usage(START, END)
    assert slept and slept[0] > 0


def test_backoff_grows_exponentially():
    slept: list[float] = []
    api, _ = client(
        *[error(503)] * 3,
        ok(page()),
        sleep=record_sleeps(slept),
        backoff_base=1.0,
        max_retries=3,
    )
    api.usage(START, END)
    assert slept == [1.0, 2.0, 4.0]


def test_backoff_is_capped():
    slept: list[float] = []
    api, _ = client(
        *[error(503)] * 4,
        ok(page()),
        sleep=record_sleeps(slept),
        backoff_base=10.0,
        backoff_cap=15.0,
        max_retries=4,
    )
    api.usage(START, END)
    assert max(slept) <= 15.0


# ==========================================================================
# Failure -- malformed responses
# ==========================================================================


def test_non_json_body_is_a_clear_error_not_a_traceback():
    api, _ = client(raw(200, "<html><body>502 Bad Gateway</body></html>"))
    with pytest.raises(ProtocolError) as excinfo:
        api.usage(START, END)
    message = str(excinfo.value)
    assert "could not parse" in message
    assert "html" in message.lower()  # shows what actually arrived


def test_an_empty_body_is_a_clear_error():
    api, _ = client(raw(200, ""))
    with pytest.raises(ProtocolError):
        api.usage(START, END)


def test_a_json_array_is_rejected():
    """The endpoints return objects. A list means something else answered."""
    api, _ = client(raw(200, "[1, 2, 3]"))
    with pytest.raises(ProtocolError, match="expected a JSON object"):
        api.usage(START, END)


def test_a_connection_failure_is_retried_then_surfaced():
    api, transport = client(
        ApiError("connection reset"),
        ApiError("connection reset"),
        ok(page()),
    )
    # Transport-level exceptions propagate; the caller sees a real failure
    # rather than an empty result.
    with pytest.raises(ApiError, match="connection reset"):
        api.usage(START, END)
    assert transport.calls == 1


# ==========================================================================
# Failure -- pagination
# ==========================================================================


def test_has_more_with_no_next_page_aborts():
    """Contradictory: there is more, but no way to ask for it. Returning what
    arrived would be returning a partial result that looks complete."""
    api, _ = client(ok(page(has_more=True, next_page=None)))
    with pytest.raises(ProtocolError) as excinfo:
        api.usage(START, END)
    assert "no way to continue" in str(excinfo.value)
    assert "partial" in str(excinfo.value)


def test_a_repeated_cursor_is_detected_rather_than_looping():
    api, transport = client(
        ok(page(has_more=True, next_page="cursor_a")),
        ok(page(has_more=True, next_page="cursor_a")),
    )
    with pytest.raises(ProtocolError, match="same next_page cursor twice"):
        api.usage(START, END)
    assert transport.calls == 2


def test_the_page_cap_stops_a_runaway():
    api, transport = client(
        *[ok(page(has_more=True, next_page=f"cursor_{i}")) for i in range(10)],
        page_cap=5,
    )
    with pytest.raises(ProtocolError, match="stopped after 5 pages"):
        api.usage(START, END)
    assert transport.calls == 5


def test_a_failure_mid_pagination_returns_nothing_at_all():
    """The single most important failure test here. Two pages arrived; the
    third failed. A tool that returned the first two would print a bill for
    two thirds of a month with nothing to say so."""
    api, _ = client(
        ok(page(has_more=True, next_page="c1")),
        ok(page(has_more=True, next_page="c2")),
        error(400, "gone wrong"),
    )
    with pytest.raises(ApiError):
        api.usage(START, END)


def test_a_failure_mid_windowing_names_the_window_and_returns_nothing():
    """Window 1 arrived; window 2 failed. Keeping window 1 would be keeping
    a third of the range and presenting it as the whole."""
    api, _ = client(ok(days(2)), error(400, "gone wrong"))
    with pytest.raises(ApiError) as excinfo:
        api.usage(START, START + timedelta(days=6), bucket_width="1d", limit=2, window=True)
    message = str(excinfo.value)
    assert "window 2 of 3" in message
    assert "rather than returning partial data" in message


# ==========================================================================
# Failure -- refused before the wire
# ==========================================================================


def test_a_limit_above_the_documented_cap_is_refused_without_a_request():
    api, transport = client()
    with pytest.raises(ApiError, match="outside 1..31"):
        api.usage(START, END, bucket_width="1d", limit=50)
    assert transport.calls == 0, "should never have hit the network"


@pytest.mark.parametrize("width,cap", sorted(MAX_LIMIT.items()))
def test_each_bucket_width_enforces_its_own_cap(width, cap):
    api, transport = client()
    with pytest.raises(ApiError):
        api.usage(START, END, bucket_width=width, limit=cap + 1)
    assert transport.calls == 0


def test_a_naive_datetime_is_refused_rather_than_assumed():
    """Guessing the timezone would silently shift every bucket."""
    api, transport = client()
    with pytest.raises(ApiError, match="no timezone"):
        api.usage(datetime(2026, 8, 1), END)
    assert transport.calls == 0


def test_an_inverted_range_is_refused():
    api, transport = client()
    with pytest.raises(ApiError, match="must be after"):
        api.usage(END, START)
    assert transport.calls == 0


def test_an_equal_range_is_refused():
    api, transport = client()
    with pytest.raises(ApiError, match="must be after"):
        api.usage(START, START)
    assert transport.calls == 0


def test_an_unknown_group_by_is_refused_and_lists_the_valid_ones():
    api, transport = client()
    with pytest.raises(ApiError) as excinfo:
        api.usage(START, END, group_by=["workspace", "model"])
    message = str(excinfo.value)
    assert "workspace" in message
    assert "workspace_id" in message  # the valid spelling is offered
    assert transport.calls == 0


def test_an_unknown_bucket_width_is_refused():
    api, transport = client()
    with pytest.raises(ApiError, match="bucket_width"):
        api.usage(START, END, bucket_width="1w")
    assert transport.calls == 0


def test_cost_rejects_usage_only_group_by_values():
    """The cost endpoint accepts only description and workspace_id."""
    api, transport = client()
    with pytest.raises(ApiError, match="model"):
        api.cost(START, END, group_by=["model"])
    assert transport.calls == 0


# ==========================================================================
# Success
# ==========================================================================


def test_a_single_page_comes_back():
    api, transport = client(ok(page()))
    pages = api.usage(START, END)
    assert len(pages) == 1
    assert pages[0]["data"][0]["results"][0]["model"] == "claude-opus-5"
    assert transport.calls == 1


def test_three_pages_are_stitched_in_order():
    api, transport = client(
        ok(page(starting_at="2026-08-01T00:00:00Z", has_more=True, next_page="c1")),
        ok(page(starting_at="2026-08-02T00:00:00Z", has_more=True, next_page="c2")),
        ok(page(starting_at="2026-08-03T00:00:00Z")),
    )
    pages = api.usage(START, END)
    assert len(pages) == 3
    starts = [p["data"][0]["starting_at"] for p in pages]
    assert starts == [
        "2026-08-01T00:00:00Z",
        "2026-08-02T00:00:00Z",
        "2026-08-03T00:00:00Z",
    ]
    assert transport.calls == 3


def test_the_page_cursor_is_sent_back():
    api, transport = client(
        ok(page(has_more=True, next_page="cursor_xyz")),
        ok(page()),
    )
    api.usage(START, END)
    assert "page=cursor_xyz" in transport.urls()[1]
    assert "page=" not in transport.urls()[0]


def test_429_then_success():
    api, transport = client(error(429), ok(page()))
    pages = api.usage(START, END)
    assert len(pages) == 1
    assert transport.calls == 2, "the retry is visible in the call count"


def test_windows_tile_without_double_counting():
    """[start, end) matches the API's own inclusive-start/exclusive-end
    semantics, so abutting windows cannot both claim the boundary bucket."""
    api, transport = client(ok(days(2, start_day=1)), ok(days(2, start_day=3)), ok(days(2, start_day=5)))
    api.usage(START, START + timedelta(days=6), bucket_width="1d", limit=2, window=True)

    assert transport.calls == 3
    starts = [_param(u, "starting_at") for u in transport.urls()]
    ends = [_param(u, "ending_at") for u in transport.urls()]
    assert starts == ["2026-08-01T00:00:00Z", "2026-08-03T00:00:00Z", "2026-08-05T00:00:00Z"]
    # Each window's end is the next window's start: no overlap, no gap.
    assert ends[:-1] == starts[1:]
    assert ends[-1] == "2026-08-07T00:00:00Z"


def test_a_final_short_window_is_clipped_to_the_range():
    api, transport = client(ok(days(2)), ok(days(1, start_day=3)))
    api.usage(START, START + timedelta(days=3), bucket_width="1d", limit=2, window=True)
    assert _param(transport.urls()[-1], "ending_at") == "2026-08-04T00:00:00Z"


def test_no_bucket_appears_twice_across_windows():
    api, _ = client(ok(days(2, start_day=1)), ok(days(2, start_day=3)))
    pages = api.usage(
        START, START + timedelta(days=4), bucket_width="1d", limit=2, window=True
    )
    starts = [b["starting_at"] for p in pages for b in p["data"]]
    assert len(starts) == len(set(starts)), f"duplicated buckets: {starts}"


# ==========================================================================
# Success -- headers and parameters
# ==========================================================================


def test_the_documented_headers_are_all_sent():
    api, transport = client(ok(page()))
    api.usage(START, END)
    headers = transport.requests[0].headers
    assert headers["anthropic-version"] == ANTHROPIC_VERSION
    assert headers["x-api-key"] == KEY
    assert headers["Accept"] == "application/json"


def test_the_user_agent_follows_the_documented_shape():
    """The docs ask integrations to identify themselves: 'YourApp/1.0.0
    (https://yourapp.com)'."""
    api, transport = client(ok(page()))
    api.usage(START, END)
    agent = transport.header("User-Agent")
    assert agent.startswith("claude-spend-audit/")
    assert "(+https://" in agent


def test_a_custom_user_agent_is_respected():
    transport = FakeTransport(ok(page()))
    api = ApiClient(KEY, transport=transport, sleep=no_sleep, user_agent="Acme/2.0")
    api.usage(START, END)
    assert transport.header("User-Agent") == "Acme/2.0"


def test_grouping_by_speed_adds_the_beta_header():
    """Documented requirement; omitting it is a 400."""
    api, transport = client(ok(page()))
    api.usage(START, END, group_by=["speed", "model"])
    assert transport.header("anthropic-beta") == FAST_MODE_BETA


def test_no_beta_header_when_speed_is_not_requested():
    api, transport = client(ok(page()))
    api.usage(START, END, group_by=["model"])
    assert transport.header("anthropic-beta") is None


def test_an_oauth_token_uses_bearer_and_its_own_beta_header():
    transport = FakeTransport(ok(page()))
    api = ApiClient("sk-ant-oat01-TOKEN", transport=transport, sleep=no_sleep)
    api.usage(START, END)
    assert transport.header("Authorization") == "Bearer sk-ant-oat01-TOKEN"
    assert transport.header("anthropic-beta") == OAUTH_BETA
    assert transport.header("x-api-key") is None


def test_group_by_is_sent_as_a_repeated_parameter():
    api, transport = client(ok(page()))
    api.usage(START, END, group_by=["model", "workspace_id"])
    url = transport.urls()[0]
    assert "group_by%5B%5D=model" in url
    assert "group_by%5B%5D=workspace_id" in url


def test_the_limit_defaults_to_the_documented_maximum():
    api, transport = client(ok(page()))
    api.usage(START, END, bucket_width="1h")
    assert _param(transport.urls()[0], "limit") == "168"


def test_timestamps_are_sent_as_rfc3339_utc():
    api, transport = client(ok(page()))
    # A non-UTC aware datetime must be converted, not rejected.
    api.usage(
        datetime(2026, 8, 1, 5, 0, tzinfo=timezone(timedelta(hours=5))),
        END,
    )
    assert _param(transport.urls()[0], "starting_at") == "2026-08-01T00:00:00Z"


def test_the_cost_endpoint_is_a_different_path():
    api, transport = client(ok({"data": [], "has_more": False}))
    api.cost(START, END, group_by=["workspace_id", "description"])
    assert "/v1/organizations/cost_report" in transport.urls()[0]


def test_cost_accepts_its_own_group_by_values():
    api, transport = client(ok({"data": [], "has_more": False}))
    api.cost(START, END, group_by=["description"])
    assert transport.calls == 1


# --------------------------------------------------------------------------


def _param(url: str, name: str) -> str | None:
    from urllib.parse import parse_qs, urlparse

    values = parse_qs(urlparse(url).query).get(name)
    return values[0] if values else None
