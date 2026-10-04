"""Synthetic Claude Code Analytics API responses, in the documented shape.

Field names are from Anthropic's reference for
/v1/organizations/usage_report/claude_code, read 2026-10-03. No real account
has been seen; every figure below can be checked by hand.

Rates (pricing/2026-09.json): Opus 5 $5 in, $0.50 cache read, $6.25 5m write
(the rate an unsplit cache write is priced at), $25 out. Sonnet 5 $2 in, $10
out. Three days from 2026-08-03, three actors:

    alice@example.com  api           Opus 5, daily:
                                       1.0M in x $5 = $5.00, 4.0M read x $0.50 = $2.00,
                                       0.2M write x $6.25 = $1.25, 0.1M out x $25 = $2.50
                                       = $10.75; on day 3 instead 10M in, 1M out:
                                       $50 + $2 + $1.25 + $25 = $78.25
                                     Sonnet 5 on day 1: 1M in + 0.1M out = $3.00
                                     sessions 2, 2, 1
                                     => $13.75 + $10.75 + $78.25 = $102.75
    api key "ci-bot"   api           Sonnet 5, daily 0.5M in + 50k out = $1.50,
                                     3 sessions a day  => $4.50
    carol@example.com  subscription  Opus 5, daily 2M in + 0.2M out = $15.00,
                                     1 session a day   => $45.00

    estimated, all actors  $152.25    API-billed (alice + ci-bot)  $107.25
    subscription share     $45.00 / $152.25 = 29.6%  -> "30%"

Per-session cost by actor-day: 6.875, 5.375, 78.25 (alice); 0.50 x3 (ci-bot);
15.00 x3 (carol). Median of the nine is 6.875; 5x is 34.375, so only alice's
day 3 (78.25, 11.4x) is flagged by K2.

Tool decisions. Alice, daily: Edit 10 accepted / 10 rejected, Write 2 / 3.
Carol, daily: Edit 20 / 2, MultiEdit 5 / 0. ci-bot: none.
    alice  36 accepted of 75 = 48%  -> flagged by K1 (under 70%, 20+ proposals)
    carol  75 of 81 = 93%
    all    111 of 156 = 71%
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from claude_spend_audit.ingest.api_client import claude_code_envelope

START = datetime(2026, 8, 3, tzinfo=timezone.utc)
END = START + timedelta(days=3)
FETCHED = datetime(2026, 10, 3, tzinfo=timezone.utc)


def _model(model, *, inp=0, read=0, write=0, out=0, cents, currency="USD"):
    return {
        "model": model,
        "tokens": {"input": inp, "output": out, "cache_read": read, "cache_creation": write},
        "estimated_cost": {"currency": currency, "amount": cents},
    }


def _tools(edit=(0, 0), multi=(0, 0), write=(0, 0), notebook=(0, 0)):
    def pair(p):
        return {"accepted": p[0], "rejected": p[1]}

    return {
        "edit_tool": pair(edit),
        "multi_edit_tool": pair(multi),
        "write_tool": pair(write),
        "notebook_edit_tool": pair(notebook),
    }


def _record(day, actor, customer, sessions, tools, models):
    return {
        "date": (START + timedelta(days=day)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "actor": actor,
        "organization_id": "00000000-0000-0000-0000-000000000000",
        "customer_type": customer,
        "terminal_type": "vscode",
        "core_metrics": {
            "num_sessions": sessions,
            "lines_of_code": {"added": 100, "removed": 10},
            "commits_by_claude_code": 1,
            "pull_requests_by_claude_code": 0,
        },
        "tool_actions": tools,
        "model_breakdown": models,
    }


ALICE = {"type": "user_actor", "email_address": "alice@example.com"}
BOT = {"type": "api_actor", "api_key_name": "ci-bot"}
CAROL = {"type": "user_actor", "email_address": "carol@example.com"}


def records(day: int, *, inflate: Decimal | None = None, currency: str = "USD") -> list[dict]:
    """One day's records. ``inflate`` scales the API rows' estimated cost,
    to make Anthropic's estimate disagree with ours."""

    def c(amount: int, api: bool = True) -> Any:
        if inflate is not None and api:
            return str(Decimal(amount) * inflate)
        return amount

    alice_models = [
        _model("claude-opus-5", inp=10_000_000 if day == 2 else 1_000_000, read=4_000_000,
               write=200_000, out=1_000_000 if day == 2 else 100_000,
               cents=c(7825 if day == 2 else 1075), currency=currency),
    ]
    if day == 0:
        alice_models.append(_model("claude-sonnet-5", inp=1_000_000, out=100_000, cents=c(300)))
    return [
        _record(day, ALICE, "api", 1 if day == 2 else 2,
                _tools(edit=(10, 10), write=(2, 3)), alice_models),
        _record(day, BOT, "api", 3, _tools(),
                [_model("claude-sonnet-5", inp=500_000, out=50_000, cents=c(150))]),
        _record(day, CAROL, "subscription", 1, _tools(edit=(20, 2), multi=(5, 0)),
                [_model("claude-opus-5", inp=2_000_000, out=200_000, cents=c(1500, api=False))]),
    ]


def page(data: list, *, has_more: bool = False, next_page: str | None = None) -> dict:
    return {"data": data, "has_more": has_more, "next_page": next_page}


def fetched(
    *,
    inflate: Decimal | None = None,
    currency: str = "USD",
    fetched_at: datetime = FETCHED,
) -> dict[str, Any]:
    """A whole saved fetch, as `fetch --source claude-code` would write it.
    Day 2 arrives on two pages, as a large organisation's would."""
    day1 = records(1, inflate=inflate, currency=currency)
    days = [
        {"date": "2026-08-03", "pages": [page(records(0, inflate=inflate, currency=currency))]},
        {"date": "2026-08-04", "pages": [page(day1[:2], has_more=True, next_page="p2"),
                                          page(day1[2:])]},
        {"date": "2026-08-05", "pages": [page(records(2, inflate=inflate, currency=currency))]},
    ]
    return claude_code_envelope(
        starting_at=START, ending_at=END, days=days, version="0.1.0", fetched_at=fetched_at
    )
