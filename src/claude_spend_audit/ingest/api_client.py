"""Fetching half of the Usage & Cost Admin API path.

PROJECT_BRIEF.md §6: "M4 -- Admin API ingest. Pagination, windowing around the
bucket caps, retry with backoff. Set a ``User-Agent`` per the docs."

``api_json.py`` already turns responses into a ``Frame``; this module only gets
the pages. The split matters: everything below is about HTTP, and nothing below
knows what a token bucket is.

**Why stdlib.** §7 names ``httpx``. This is the one feature that asks someone
for an org-wide admin credential, and the moment they are deciding whether to
hand it over is the moment an empty dependency list is worth most -- they can
read the whole code path. ``urllib.request`` does authenticated GET with
headers and timeouts, which is all this needs.

**Two rules that shape the rest.**

*The credential is never returned, logged, or interpolated.* It lives behind
``Credential``, whose ``__repr__`` and ``__str__`` are redacted, and which is
never placed in a URL. Tests assert it cannot appear in an exception.

*Partial data is never returned.* If a page or window fails after retries, the
whole fetch raises. Half a month that looks like a whole one is the worst
failure mode available to a tool that prints a bill.
"""

from __future__ import annotations

import json
import random
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

BASE_URL = "https://api.anthropic.com"
USAGE_PATH = "/v1/organizations/usage_report/messages"
COST_PATH = "/v1/organizations/cost_report"

ANTHROPIC_VERSION = "2023-06-01"

#: Documented max for the ``limit`` parameter, per bucket width. These cap
#: buckets *per page*, not the range -- the docs' own pagination example pulls
#: a 31-day range with limit=7 across several pages.
MAX_LIMIT: Mapping[str, int] = {"1m": 1440, "1h": 168, "1d": 31}

#: Bucket length, used to size windows.
BUCKET_SECONDS: Mapping[str, int] = {"1m": 60, "1h": 3600, "1d": 86400}

#: group_by values the usage endpoint accepts.
USAGE_GROUP_BY = frozenset(
    {
        "account_id",
        "api_key_id",
        "context_window",
        "inference_geo",
        "model",
        "service_account_id",
        "service_tier",
        "speed",
        "workspace_id",
    }
)
COST_GROUP_BY = frozenset({"description", "workspace_id"})

#: Grouping by speed requires this beta header; forgetting it is a 400.
FAST_MODE_BETA = "fast-mode-2026-02-01"
OAUTH_BETA = "oauth-2025-04-20"

#: Retried. A 4xx other than 429 does not become correct by repetition.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: Stops a server that keeps saying has_more from looping forever.
DEFAULT_PAGE_CAP = 1000


class ApiError(Exception):
    """Any failure on the API path. Never carries the credential."""


class AuthError(ApiError):
    """401/403. Separated because the remedy is specific and documented."""


class RateLimited(ApiError):
    """429 that survived every retry."""


class ProtocolError(ApiError):
    """The response did not make sense: bad JSON, or contradictory paging."""


# --------------------------------------------------------------------------
# Credential
# --------------------------------------------------------------------------


class Credential:
    """An admin key or OAuth token that refuses to render itself.

    The value is reachable only through ``headers()``. ``repr`` and ``str`` are
    redacted so it cannot reach a log, a traceback, or an f-string by accident
    -- which is the realistic way a secret escapes, not by anyone deciding to
    print it.
    """

    __slots__ = ("_value", "kind")

    def __init__(self, value: str):
        value = (value or "").strip()
        if not value:
            raise AuthError("credential is empty")
        self._value = value
        # OAuth tokens go on Authorization: Bearer with a beta header; admin
        # and service keys go on x-api-key.
        self.kind = "oauth" if value.startswith(("sk-ant-oat", "oauth-")) else "api_key"

    def headers(self) -> dict[str, str]:
        if self.kind == "oauth":
            return {
                "Authorization": f"Bearer {self._value}",
                "anthropic-beta": OAUTH_BETA,
            }
        return {"x-api-key": self._value}

    def __repr__(self) -> str:
        return f"<Credential {self.kind} [redacted]>"

    __str__ = __repr__


def redact(text: str, credential: Credential | None) -> str:
    """Belt and braces: strip the secret from text on its way to an exception.

    ``Credential`` already refuses to render, but an API could echo a key back
    in an error body, and that body ends up in our message.
    """
    if credential is None:
        return text
    secret = credential._value  # noqa: SLF001 - the one place this is read
    return text.replace(secret, "[redacted]") if secret else text


# --------------------------------------------------------------------------
# Transport -- the seam every test drives
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Request:
    url: str
    headers: Mapping[str, str]
    method: str = "GET"
    timeout: float = 30.0


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: str
    headers: Mapping[str, str] = field(default_factory=dict)


Transport = Callable[[Request], Response]


def urllib_transport(request: Request) -> Response:
    """The real one. Kept tiny so the whole network surface is readable."""
    req = urllib.request.Request(
        request.url, headers=dict(request.headers), method=request.method
    )
    try:
        with urllib.request.urlopen(req, timeout=request.timeout) as resp:
            return Response(
                status=resp.status,
                body=resp.read().decode("utf-8", errors="replace"),
                headers={k.lower(): v for k, v in resp.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        # An HTTP error status is a Response, not an exception -- the retry
        # logic decides what it means.
        return Response(
            status=exc.code,
            body=exc.read().decode("utf-8", errors="replace"),
            headers={k.lower(): v for k, v in (exc.headers or {}).items()},
        )
    except urllib.error.URLError as exc:
        raise ApiError(f"could not reach {BASE_URL}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ApiError("request timed out") from exc


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


def _rfc3339(moment: datetime, *, name: str) -> str:
    """Format for the wire, refusing anything ambiguous.

    A naive datetime is rejected rather than assumed to be UTC: the caller
    means *some* instant, and guessing which one silently shifts every bucket.
    """
    if moment.tzinfo is None:
        raise ApiError(
            f"{name} has no timezone. Pass an aware datetime -- a naive one "
            "would be guessed at, and guessing shifts every bucket."
        )
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ApiClient:
    """Fetches raw pages. Knows nothing about token buckets."""

    def __init__(
        self,
        credential: Credential | str,
        *,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] | None = None,
        user_agent: str | None = None,
        base_url: str = BASE_URL,
        max_retries: int = 4,
        backoff_base: float = 0.5,
        backoff_cap: float = 30.0,
        page_cap: int = DEFAULT_PAGE_CAP,
        timeout: float = 30.0,
        jitter: Callable[[], float] = random.random,
    ):
        self.credential = (
            credential if isinstance(credential, Credential) else Credential(credential)
        )
        self._transport = transport
        # Injected so tests do not actually wait out a backoff.
        self._sleep = sleep if sleep is not None else __import__("time").sleep
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.page_cap = page_cap
        self.timeout = timeout
        self._jitter = jitter

        from .. import __version__

        self.user_agent = user_agent or (
            f"claude-spend-audit/{__version__} "
            "(+https://github.com/anthropics/claude-spend-audit)"
        )

    def __repr__(self) -> str:
        return f"<ApiClient {self.base_url} {self.credential!r}>"

    # -- public ----------------------------------------------------------

    def usage(
        self,
        starting_at: datetime,
        ending_at: datetime,
        *,
        bucket_width: str = "1d",
        group_by: Sequence[str] = (),
        limit: int | None = None,
        window: bool = False,
        extra: Mapping[str, Any] | None = None,
    ) -> list[dict]:
        """Every page of the usage report for this range."""
        if bucket_width not in MAX_LIMIT:
            raise ApiError(
                f"bucket_width {bucket_width!r} is not one of "
                f"{', '.join(sorted(MAX_LIMIT))}"
            )
        unknown = set(group_by) - USAGE_GROUP_BY
        if unknown:
            raise ApiError(
                f"unknown group_by value(s): {', '.join(sorted(unknown))}. "
                f"Valid: {', '.join(sorted(USAGE_GROUP_BY))}"
            )
        limit = self._checked_limit(limit, bucket_width)

        params: dict[str, Any] = {"bucket_width": bucket_width, "limit": limit}
        if group_by:
            params["group_by[]"] = list(group_by)
        if extra:
            params.update(extra)

        headers = {}
        if "speed" in group_by:
            # Documented requirement; omitting it is a 400.
            headers["anthropic-beta"] = FAST_MODE_BETA

        return self._fetch_range(
            USAGE_PATH,
            starting_at,
            ending_at,
            params,
            extra_headers=headers,
            window_buckets=limit if window else None,
            bucket_width=bucket_width,
        )

    def cost(
        self,
        starting_at: datetime,
        ending_at: datetime,
        *,
        group_by: Sequence[str] = (),
        limit: int | None = None,
        window: bool = False,
    ) -> list[dict]:
        """Every page of the cost report. Daily granularity only."""
        unknown = set(group_by) - COST_GROUP_BY
        if unknown:
            raise ApiError(
                f"unknown group_by value(s): {', '.join(sorted(unknown))}. "
                f"Valid: {', '.join(sorted(COST_GROUP_BY))}"
            )
        limit = self._checked_limit(limit, "1d")
        params: dict[str, Any] = {"limit": limit}
        if group_by:
            params["group_by[]"] = list(group_by)

        return self._fetch_range(
            COST_PATH,
            starting_at,
            ending_at,
            params,
            window_buckets=limit if window else None,
            bucket_width="1d",
        )

    # -- internals -------------------------------------------------------

    def _checked_limit(self, limit: int | None, bucket_width: str) -> int:
        cap = MAX_LIMIT[bucket_width]
        if limit is None:
            return cap
        if limit < 1 or limit > cap:
            raise ApiError(
                f"limit {limit} is outside 1..{cap} for bucket_width "
                f"{bucket_width!r}. Rejected before sending, since the API "
                "would only return a 400."
            )
        return limit

    def _fetch_range(
        self,
        path: str,
        starting_at: datetime,
        ending_at: datetime,
        params: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
        window_buckets: int | None,
        bucket_width: str,
    ) -> list[dict]:
        # Validate awareness first. Comparing a naive datetime to an aware one
        # raises TypeError, which would bury the actionable message below.
        _rfc3339(starting_at, name="starting_at")
        _rfc3339(ending_at, name="ending_at")
        if ending_at <= starting_at:
            raise ApiError(
                f"ending_at ({ending_at.isoformat()}) must be after starting_at "
                f"({starting_at.isoformat()})"
            )

        windows = (
            list(self._windows(starting_at, ending_at, window_buckets, bucket_width))
            if window_buckets
            else [(starting_at, ending_at)]
        )

        pages: list[dict] = []
        for index, (start, end) in enumerate(windows, start=1):
            try:
                pages.extend(self._paginate(path, start, end, params, extra_headers))
            except ApiError as exc:
                if len(windows) == 1:
                    raise
                # Say which window died. Returning the windows that did work
                # would be returning a partial month that looks whole.
                raise ApiError(
                    f"window {index} of {len(windows)} "
                    f"({start:%Y-%m-%d} to {end:%Y-%m-%d}) failed, so the whole "
                    f"fetch is abandoned rather than returning partial data: {exc}"
                ) from exc
        return pages

    @staticmethod
    def _windows(
        starting_at: datetime,
        ending_at: datetime,
        buckets: int,
        bucket_width: str,
    ) -> Iterable[tuple[datetime, datetime]]:
        """Tile the range as [start, end), matching the API's own semantics.

        ``starting_at`` is inclusive and ``ending_at`` exclusive, so abutting
        windows cannot double-count the bucket on their shared boundary.
        """
        span = timedelta(seconds=BUCKET_SECONDS[bucket_width] * buckets)
        cursor = starting_at
        while cursor < ending_at:
            nxt = min(cursor + span, ending_at)
            yield cursor, nxt
            cursor = nxt

    def _paginate(
        self,
        path: str,
        starting_at: datetime,
        ending_at: datetime,
        params: Mapping[str, Any],
        extra_headers: Mapping[str, str] | None,
    ) -> list[dict]:
        pages: list[dict] = []
        seen_cursors: set[str] = set()
        page_token: str | None = None

        while True:
            query = dict(params)
            query["starting_at"] = _rfc3339(starting_at, name="starting_at")
            query["ending_at"] = _rfc3339(ending_at, name="ending_at")
            if page_token:
                query["page"] = page_token

            payload = self._get(path, query, extra_headers)
            pages.append(payload)

            if not payload.get("has_more"):
                return pages

            nxt = payload.get("next_page")
            if not nxt:
                raise ProtocolError(
                    "the API reported has_more=true but gave no next_page, so "
                    "there is no way to continue. Refusing to return a partial "
                    "result that would look complete."
                )
            if nxt in seen_cursors:
                raise ProtocolError(
                    f"the API returned the same next_page cursor twice "
                    f"({nxt[:16]}...), which would loop forever. Aborting."
                )
            seen_cursors.add(nxt)
            page_token = nxt

            if len(pages) >= self.page_cap:
                raise ProtocolError(
                    f"stopped after {self.page_cap} pages without has_more "
                    "going false. Narrow the range, or raise page_cap if this "
                    "is genuinely that much data."
                )

    def _get(
        self,
        path: str,
        query: Mapping[str, Any],
        extra_headers: Mapping[str, str] | None,
    ) -> dict:
        # The credential goes in a header. Never in the URL, where it would
        # reach proxy logs, browser history and error messages.
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(query, doseq=True)}"
        headers = {
            "anthropic-version": ANTHROPIC_VERSION,
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        headers.update(extra_headers or {})
        headers.update(self.credential.headers())

        last: Response | None = None
        for attempt in range(self.max_retries + 1):
            response = self._transport(
                Request(url=url, headers=headers, method="GET", timeout=self.timeout)
            )
            last = response

            if response.status == 200:
                return self._parse(response)

            if response.status in (401, 403):
                raise AuthError(self._auth_message(response))

            if response.status not in RETRYABLE_STATUS:
                raise ApiError(
                    f"HTTP {response.status} from {path}: "
                    f"{redact(self._detail(response), self.credential)}"
                )

            if attempt == self.max_retries:
                break
            self._sleep(self._backoff(attempt, response))

        assert last is not None
        detail = redact(self._detail(last), self.credential)
        attempts = self.max_retries + 1
        if last.status == 429:
            raise RateLimited(
                f"rate limited by the API after {attempts} attempts. The docs "
                f"support about one request per minute sustained. {detail}"
            )
        raise ApiError(f"HTTP {last.status} after {attempts} attempts: {detail}")

    def _backoff(self, attempt: int, response: Response) -> float:
        """Honour retry-after when given; otherwise exponential with jitter."""
        header = response.headers.get("retry-after")
        if header:
            try:
                # RFC 9110: delay-seconds is a non-negative integer. Parsing it
                # as one is both more correct and keeps the package free of
                # float() calls, which the money invariant bans outright.
                return min(int(header.strip()), self.backoff_cap)
            except ValueError:
                pass  # the HTTP-date form; fall through to backoff
        return min(self.backoff_base * (2**attempt) + self._jitter(), self.backoff_cap)

    def _parse(self, response: Response) -> dict:
        try:
            payload = json.loads(response.body)
        except json.JSONDecodeError as exc:
            snippet = redact(response.body[:200], self.credential)
            raise ProtocolError(
                f"expected JSON but could not parse the response: {exc}. "
                f"First 200 characters: {snippet!r}"
            ) from exc
        if not isinstance(payload, dict):
            raise ProtocolError(
                f"expected a JSON object, got {type(payload).__name__}"
            )
        return payload

    def _auth_message(self, response: Response) -> str:
        detail = redact(self._detail(response), self.credential)
        if response.status == 403:
            return (
                "the API rejected this credential (403). Workspace-scoped API "
                "keys do not work on the Admin API -- it needs an Admin API key "
                "(sk-ant-admin01-...), an OAuth token with the org:admin scope, "
                f"or an unscoped personal or service account key. {detail}"
            )
        return (
            "the API rejected this credential (401). Check the key is current "
            "and belongs to this organization. Claude Enterprise organizations "
            "use a different API and an Analytics API key, and carry no Admin "
            f"API keys at all. {detail}"
        )

    @staticmethod
    def _detail(response: Response) -> str:
        body = (response.body or "").strip()
        if not body:
            return "The response body was empty."
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return f"Response body began: {body[:200]!r}"
        message = None
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict):
                message = error.get("message")
        return f"API said: {message}" if message else f"Response: {body[:200]}"


# --------------------------------------------------------------------------
# Saved artifacts
# --------------------------------------------------------------------------

#: Marks a file written by `fetch`. Lets `audit` recognise one without
#: guessing, and carries the one thing the responses cannot report about
#: themselves: which dimensions were actually requested.
ENVELOPE_KIND = "claude-spend-audit/fetch"


def envelope(
    *,
    endpoint: str,
    starting_at: datetime,
    ending_at: datetime,
    bucket_width: str | None,
    group_by: Sequence[str],
    pages: Sequence[dict],
    version: str,
    fetched_at: datetime | None = None,
) -> dict:
    """Wrap raw pages with just enough provenance to be re-readable.

    `group_by` is the load-bearing field. A response cannot report which
    dimensions were requested -- an ungrouped dimension and a genuinely-null
    one both come back as null -- so without recording it here, re-reading the
    file later would have to guess, and `Frame.grouped_by` would be a lie.

    Deliberately contains no credential, and there is a test asserting it.
    """
    return {
        "kind": ENVELOPE_KIND,
        "version": version,
        "fetched_at": (fetched_at or datetime.now(timezone.utc)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "endpoint": endpoint,
        "request": {
            "starting_at": _rfc3339(starting_at, name="starting_at"),
            "ending_at": _rfc3339(ending_at, name="ending_at"),
            "bucket_width": bucket_width,
            "group_by": list(group_by),
        },
        "pages": list(pages),
    }


def is_envelope(payload: object) -> bool:
    return isinstance(payload, Mapping) and payload.get("kind") == ENVELOPE_KIND
