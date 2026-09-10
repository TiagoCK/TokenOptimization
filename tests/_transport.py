"""A scripted transport, so no test touches the network.

Every API test drives one of these. It records the requests it was given, which
is how the tests assert on headers, on the credential never reaching a URL, and
on *how many times* a call was retried -- the last being the only way to prove
a 400 was not retried.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Sequence

from claude_spend_audit.ingest.api_client import Request, Response


class FakeTransport:
    """Returns a scripted sequence of responses, recording every request.

    A response may also be an exception instance, which is raised instead --
    that is how connection failures are simulated.
    """

    def __init__(self, *responses: Response | Exception):
        self._responses: list[Response | Exception] = list(responses)
        self.requests: list[Request] = []

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError(
                f"transport called {len(self.requests)} times but only "
                f"{len(self.requests) - 1} responses were scripted"
            )
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def last(self) -> Request:
        return self.requests[-1]

    def urls(self) -> list[str]:
        return [r.url for r in self.requests]

    def header(self, name: str, index: int = 0) -> str | None:
        return self.requests[index].headers.get(name)


def ok(payload: dict[str, Any], **headers: str) -> Response:
    return Response(status=200, body=json.dumps(payload), headers=headers)


def error(status: int, message: str = "nope", **headers: str) -> Response:
    return Response(
        status=status,
        body=json.dumps({"error": {"type": "error", "message": message}}),
        headers=headers,
    )


def raw(status: int, body: str, **headers: str) -> Response:
    """A response whose body is not JSON -- an HTML error page, say."""
    return Response(status=status, body=body, headers=headers)


def page(
    *,
    starting_at: str = "2026-08-01T00:00:00Z",
    ending_at: str = "2026-08-02T00:00:00Z",
    has_more: bool = False,
    next_page: str | None = None,
    model: str = "claude-opus-5",
    uncached: int = 1_000_000,
    output: int = 200_000,
) -> dict[str, Any]:
    """One usage page in the documented response shape."""
    return {
        "data": [
            {
                "starting_at": starting_at,
                "ending_at": ending_at,
                "results": [
                    {
                        "model": model,
                        "workspace_id": "wrkspc_main",
                        "api_key_id": "apikey_main",
                        "service_tier": "standard",
                        "context_window": "0-200k",
                        "inference_geo": "global",
                        "speed": "standard",
                        "uncached_input_tokens": uncached,
                        "cache_read_input_tokens": 0,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 0,
                            "ephemeral_1h_input_tokens": 0,
                        },
                        "output_tokens": output,
                        "server_tool_use": {"web_search_requests": 0},
                    }
                ],
            }
        ],
        "has_more": has_more,
        "next_page": next_page,
    }


def days(n: int, *, start_day: int = 1, **kw) -> dict[str, Any]:
    """A page carrying n consecutive daily buckets, for window tests."""
    buckets = []
    for i in range(n):
        d = start_day + i
        buckets.append(
            {
                "starting_at": f"2026-08-{d:02d}T00:00:00Z",
                "ending_at": f"2026-08-{d + 1:02d}T00:00:00Z",
                "results": [
                    {
                        "model": "claude-opus-5",
                        "workspace_id": "wrkspc_main",
                        "service_tier": "standard",
                        "uncached_input_tokens": 1_000_000,
                        "cache_read_input_tokens": 0,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 0,
                            "ephemeral_1h_input_tokens": 0,
                        },
                        "output_tokens": 0,
                        "server_tool_use": {"web_search_requests": 0},
                    }
                ],
            }
        )
    return {"data": buckets, "has_more": False, "next_page": None, **kw}


def no_sleep(_seconds: float) -> None:
    """Injected in place of time.sleep so backoff tests are instant."""


def record_sleeps(into: list[float]) -> Callable[[float], None]:
    def _sleep(seconds: float) -> None:
        into.append(seconds)

    return _sleep
