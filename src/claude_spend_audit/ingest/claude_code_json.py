"""Normalize a saved Claude Code Analytics API fetch.

The endpoint reports one record per actor per UTC day: who (an email, or an
API key's name), whether they are a pay-as-you-go API customer or a Pro/Team
subscriber, how many sessions they ran, how many tool proposals they accepted
and rejected, and tokens with Anthropic's estimated cost per model.

Three views come out of it, each feeding the checks that suit it:

* a ``Frame`` of API-billed usage per day and model, for the usage checks.
  Subscription rows stay out: a seat is not billed per token, so a finding
  about per-token spend would not be about their bill;
* a ``SpendReport`` per actor and model, every customer type included, for
  the per-user checks. Its spend column is Anthropic's *estimate*, and says so;
* ``CodeActivity``, the per-actor-day sessions and tool decisions, for the
  Claude Code checks (K1-K3).

What the reference does not say is handled as a caveat, never a guess. The
one assumption -- that ``tokens.input`` is uncached input, as the Messages API
reports it -- is tested at audit time against Anthropic's own estimate.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

from .. import gaps as gapmod
from ..enterprise.report import SpendReport, SpendRow
from ..frame import Frame, Segment, SourceInfo, TokenBuckets, UsageRow
from .api_json import ResponseShapeError, cents, require_usd
from .schema import parse_datetime

#: The tools the endpoint reports accept/reject counts for, in its own names.
TOOLS: tuple[str, ...] = ("edit_tool", "multi_edit_tool", "write_tool", "notebook_edit_tool")
TOOL_LABELS = {
    "edit_tool": "Edit",
    "multi_edit_tool": "MultiEdit",
    "write_tool": "Write",
    "notebook_edit_tool": "NotebookEdit",
}

#: The one customer type billed per token.
API_CUSTOMER = "api"


@dataclass(frozen=True, slots=True)
class ActorDay:
    """One actor's Claude Code activity on one UTC day."""

    actor: str
    customer_type: str
    day: date
    sessions: int
    #: Tool name -> (accepted, rejected).
    tools: Mapping[str, tuple[int, int]]
    #: Anthropic's estimated cost across every model that day, in dollars.
    estimated_usd: Decimal


@dataclass(frozen=True, slots=True)
class CodeActivity:
    records: tuple[ActorDay, ...] = ()

    def relabel(self, labels: Mapping[str, str]) -> CodeActivity:
        """Apply the SpendReport's anonymous labels, so ``user-003`` is the
        same person in every section of one report."""
        return CodeActivity(
            records=tuple(replace(r, actor=labels.get(r.actor, r.actor)) for r in self.records)
        )


@dataclass(frozen=True, slots=True)
class ClaudeCodeData:
    frame: Frame
    report: SpendReport
    activity: CodeActivity
    #: Anthropic's estimated cost of the API-billed rows: the same rows the
    #: Frame holds, so the two estimates can be compared like for like.
    api_estimate_usd: Decimal
    #: Anthropic's estimated cost of every row, subscriptions included.
    total_estimate_usd: Decimal
    gaps: tuple = field(default_factory=tuple)


class ClaudeCodeFileError(ValueError):
    """A saved Claude Code fetch is missing something it must contain."""


def load_claude_code(
    envelope: Mapping[str, Any], *, source: str = "<claude-code>"
) -> ClaudeCodeData:
    days = envelope.get("days")
    if not isinstance(days, list):
        raise ClaudeCodeFileError(
            "this Claude Code fetch has no 'days' list, so there is nothing to "
            "audit. Fetch again with `fetch --source claude-code`."
        )
    info = SourceInfo(path=source, ingest_path="claude_code")

    usage: dict[tuple[datetime, str], TokenBuckets] = {}
    per_user: dict[tuple[str, str, str], dict[str, Any]] = {}
    records: list[ActorDay] = []
    api_estimate = Decimal("0")
    total_estimate = Decimal("0")
    subscription_estimate = Decimal("0")

    for entry in days:
        for page in entry.get("pages") or []:
            for record in page.get("data") or ():
                start = parse_datetime(str(record["date"]))
                day = start.date()
                actor = _label(record.get("actor") or {})
                customer = str(record.get("customer_type") or "unknown")
                day_cost = Decimal("0")

                for model_row in record.get("model_breakdown") or ():
                    model = str(model_row.get("model") or "unknown model")
                    tokens = model_row.get("tokens") or {}
                    cost = model_row.get("estimated_cost") or {}
                    require_usd(cost.get("currency"), "claude_code estimated_cost")
                    usd = cents(cost.get("amount"), "claude_code estimated_cost amount")
                    day_cost += usd

                    uncached = int(tokens.get("input") or 0)
                    read = int(tokens.get("cache_read") or 0)
                    write = int(tokens.get("cache_creation") or 0)
                    output = int(tokens.get("output") or 0)

                    if customer == API_CUSTOMER:
                        bucket = TokenBuckets(
                            uncached_input=uncached,
                            cache_read=read,
                            cache_write_unsplit=write,
                            output=output,
                        )
                        key = (start, model)
                        usage[key] = usage.get(key, TokenBuckets()) + bucket
                        api_estimate += usd

                    row = per_user.setdefault(
                        (actor, customer, model),
                        {"prompt": 0, "read": 0, "write": 0, "output": 0, "usd": Decimal("0")},
                    )
                    row["prompt"] += uncached + read + write
                    row["read"] += read
                    row["write"] += write
                    row["output"] += output
                    row["usd"] += usd

                total_estimate += day_cost
                if customer != API_CUSTOMER:
                    subscription_estimate += day_cost
                metrics = record.get("core_metrics") or {}
                actions = record.get("tool_actions") or {}
                records.append(
                    ActorDay(
                        actor=actor,
                        customer_type=customer,
                        day=day,
                        sessions=int(metrics.get("num_sessions") or 0),
                        tools={
                            tool: (
                                int((actions.get(tool) or {}).get("accepted") or 0),
                                int((actions.get(tool) or {}).get("rejected") or 0),
                            )
                            for tool in TOOLS
                        },
                        estimated_usd=day_cost,
                    )
                )

    frame = Frame(
        usage=tuple(
            UsageRow(
                start=start,
                end=start + timedelta(days=1),
                segment=Segment(model=model),
                tokens=tokens,
            )
            for (start, model), tokens in sorted(usage.items())
        ),
        grouped_by=frozenset({"model"}),
        bucket_width="1d",
        # The endpoint reports cache writes without their TTL.
        token_fields=frozenset({"uncached_input", "cache_read", "cache_write_unsplit", "output"}),
        sources=(info,),
    )

    report = SpendReport(
        rows=tuple(
            SpendRow(
                user_email=actor,
                product="Claude Code",
                model=model,
                prompt_tokens=row["prompt"],
                completion_tokens=row["output"],
                cache_read_tokens=row["read"],
                cache_write_tokens=row["write"],
                gross_spend_usd=row["usd"],
            )
            for (actor, _customer, model), row in per_user.items()
        ),
        present=frozenset(
            {
                "user_email",
                "model",
                "prompt_tokens",
                "completion_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "gross_spend_usd",
            }
        ),
        period_start=_request_time(envelope, "starting_at"),
        period_end=_request_time(envelope, "ending_at"),
        sources=(info,),
        prompt_includes_cache=True,
        spend_name="estimated cost",
    )

    found = []
    if subscription_estimate and total_estimate:
        share = f"{subscription_estimate / total_estimate:.0%}"
        kinds = tuple(sorted({r.customer_type for r in records} - {API_CUSTOMER}))
        found.append(gapmod.subscription_not_billed_per_token(share, kinds))
    recent = _recent_day(envelope, days)
    if recent:
        found.append(gapmod.claude_code_recent_day(recent))

    return ClaudeCodeData(
        frame=frame,
        report=report,
        activity=CodeActivity(records=tuple(records)),
        api_estimate_usd=api_estimate,
        total_estimate_usd=total_estimate,
        gaps=tuple(found),
    )


def _label(actor: Mapping[str, Any]) -> str:
    """The email for a user, the key's name for an API actor."""
    if actor.get("email_address"):
        return str(actor["email_address"])
    if actor.get("api_key_name"):
        return f"api key: {actor['api_key_name']}"
    return str(actor.get("type") or "unknown actor")


def _request_time(envelope: Mapping[str, Any], name: str) -> datetime | None:
    value = (envelope.get("request") or {}).get(name)
    return parse_datetime(str(value)) if value else None


def _recent_day(envelope: Mapping[str, Any], days: list) -> str | None:
    """The last day fetched, if it had not ended by the time of the fetch."""
    fetched = envelope.get("fetched_at")
    if not fetched or not days:
        return None
    fetched_at = parse_datetime(str(fetched))
    last = max(str(d.get("date")) for d in days if d.get("date"))
    end_of_day = parse_datetime(last) + timedelta(days=1)
    return last if fetched_at < end_of_day else None


__all__ = [
    "API_CUSTOMER",
    "ActorDay",
    "ClaudeCodeData",
    "ClaudeCodeFileError",
    "CodeActivity",
    "ResponseShapeError",
    "TOOLS",
    "TOOL_LABELS",
    "load_claude_code",
]
