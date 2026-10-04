"""Checks specific to Claude Code activity. All three are descriptive.

The Claude Code Analytics API adds what no other source has: sessions, and how
often people accepted or rejected the edits Claude Code proposed. That
supports a picture of how the tool is used. It supports no savings claim: a
rejected edit may be careful review, an expensive session may be one large
task done well, and a subscription seat costs the same however it is used.

So every check here is an ``observation``, enforced the same way as the
spend-report checks: ``run_code_checks`` raises ``DescriptiveOnlyViolation``
for anything else. Every dollar figure is Anthropic's *estimated* cost, and
each finding's basis says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Callable, Iterable, Sequence

from ..checks import CheckRun
from ..enterprise.checks import DESCRIPTIVE, DescriptiveOnlyViolation
from ..findings import Confidence, Evidence, Finding, rank
from ..ingest.claude_code_json import API_CUSTOMER, TOOL_LABELS, TOOLS, CodeActivity


@dataclass(frozen=True, slots=True)
class CodeConfig:
    """Thresholds for the Claude Code checks, all overridable."""

    min_impact_usd: Decimal = Decimal("1")
    #: K1 lists a user only with at least this many proposals to judge by.
    min_proposals: int = 20
    #: K1 lists users accepting less than this share of proposals.
    low_acceptance: Decimal = Decimal("0.70")
    #: K2 lists actor-days whose cost per session is this multiple of the
    #: organisation's median or more.
    session_cost_multiple: Decimal = Decimal("5")
    #: K2 needs this many actor-days with sessions before a median means much.
    min_actor_days: int = 5
    max_rows: int = 10


CodeCheckFn = Callable[[CodeActivity, CodeConfig], Iterable[Finding]]


@dataclass(frozen=True, slots=True)
class CodeCheck:
    id: str
    name: str
    run: CodeCheckFn

    @property
    def label(self) -> str:
        return f"{self.name} ({self.id})"


def _usd(amount: Decimal) -> str:
    return f"${amount:,.2f}"


def _rate(accepted: int, rejected: int) -> Decimal | None:
    total = accepted + rejected
    return Decimal(accepted) / Decimal(total) if total else None


# --------------------------------------------------------------------------
# K1 -- tool acceptance
# --------------------------------------------------------------------------


def run_tool_acceptance(activity: CodeActivity, config: CodeConfig) -> Iterable[Finding]:
    by_tool = {tool: [0, 0] for tool in TOOLS}
    by_actor: dict[str, list] = {}
    for record in activity.records:
        entry = by_actor.setdefault(record.actor, [0, 0, Decimal("0")])
        entry[2] += record.estimated_usd
        for tool, (accepted, rejected) in record.tools.items():
            by_tool[tool][0] += accepted
            by_tool[tool][1] += rejected
            entry[0] += accepted
            entry[1] += rejected

    accepted = sum(a for a, _ in by_tool.values())
    rejected = sum(r for _, r in by_tool.values())
    if accepted + rejected < config.min_proposals:
        return []
    overall = _rate(accepted, rejected)

    flagged = sorted(
        (
            (actor, a, r, _rate(a, r), cost)
            for actor, (a, r, cost) in by_actor.items()
            if a + r >= config.min_proposals and _rate(a, r) < config.low_acceptance
        ),
        key=lambda item: (item[3], -item[4], item[0]),
    )
    flagged_cost = sum((item[4] for item in flagged), Decimal("0"))

    rows = [
        (TOOL_LABELS[tool], f"{a:,}", f"{r:,}", f"{_rate(a, r):.0%}" if a + r else "-", "-")
        for tool, (a, r) in by_tool.items()
        if a + r
    ]
    rows += [
        (actor, f"{a:,}", f"{r:,}", f"{rate:.0%}", _usd(cost))
        for actor, a, r, rate, cost in flagged[: config.max_rows]
    ]

    if flagged:
        users = (
            f" {len(flagged)} user(s) with at least {config.min_proposals} "
            f"proposals accepted under {config.low_acceptance:.0%} of them; "
            f"their estimated cost over the period was {_usd(flagged_cost)}."
        )
    else:
        users = (
            f" No user with at least {config.min_proposals} proposals accepted "
            f"under {config.low_acceptance:.0%} of them."
        )

    return [
        Finding(
            check_id="K1",
            title="How often Claude Code's proposed edits were accepted",
            confidence=Confidence.OBSERVATION,
            scope=f"{overall:.0%} accepted across {accepted + rejected:,} proposals",
            summary=(
                f"{overall:.0%} of Claude Code's edit proposals were accepted "
                f"({accepted:,} of {accepted + rejected:,}).{users} {DESCRIPTIVE}"
            ),
            measured_usd=flagged_cost if flagged and flagged_cost >= config.min_impact_usd else None,
            measured_basis=(
                "Anthropic's estimated cost of the users listed, across all their "
                "Claude Code use -- not the cost of the rejected proposals, which "
                "the data cannot separate"
                if flagged and flagged_cost >= config.min_impact_usd
                else None
            ),
            evidence=Evidence(
                columns=("tool / user", "accepted", "rejected", "acceptance", "est. cost"),
                rows=tuple(rows),
                note=(
                    "Tool rows cover everyone. User rows list those below the "
                    "threshold, lowest acceptance first."
                ),
            ),
            remedy=(
                "Where acceptance is low, ask what is being rejected: unclear "
                "instructions, missing project context (a CLAUDE.md), or edits in "
                "code the tool handles poorly. Each rejected proposal still cost "
                "the tokens that produced it."
            ),
            caveats=(
                "Rejecting a proposal is often careful review, not waste. A low "
                "rate can mean a user who checks every change.",
                "Proposals accepted and then reverted count as accepted.",
            ),
        )
    ]


# --------------------------------------------------------------------------
# K2 -- cost per session
# --------------------------------------------------------------------------


def run_session_cost(activity: CodeActivity, config: CodeConfig) -> Iterable[Finding]:
    days = [r for r in activity.records if r.sessions > 0 and r.estimated_usd > 0]
    if len(days) < config.min_actor_days:
        return []
    per_session = {id(r): r.estimated_usd / r.sessions for r in days}
    typical = median(per_session.values())
    if typical <= 0:
        return []

    flagged = sorted(
        (r for r in days if per_session[id(r)] >= typical * config.session_cost_multiple),
        key=lambda r: (-per_session[id(r)], r.actor, r.day),
    )
    flagged_cost = sum((r.estimated_usd for r in flagged), Decimal("0"))
    if not flagged or flagged_cost < config.min_impact_usd:
        return []
    no_sessions = sum(
        1 for r in activity.records if r.sessions == 0 and r.estimated_usd > 0
    )

    rows = tuple(
        (
            r.actor,
            r.day.isoformat(),
            f"{r.sessions:,}",
            _usd(r.estimated_usd),
            _usd(per_session[id(r)]),
            f"{per_session[id(r)] / typical:.1f}x",
        )
        for r in flagged[: config.max_rows]
    )
    caveats = [
        "A costly session may be one large task done well. This lists where to "
        "look, not what went wrong.",
        "Sessions are counted per day: one long session spanning midnight UTC "
        "appears on both days.",
    ]
    if no_sessions:
        caveats.append(
            f"{no_sessions} actor-day(s) had estimated cost but no session "
            "recorded, and are not in the median."
        )
    return [
        Finding(
            check_id="K2",
            title="Claude Code days with unusually costly sessions",
            confidence=Confidence.OBSERVATION,
            scope=f"{len(flagged)} actor-day(s)",
            summary=(
                f"The typical (median) Claude Code session cost {_usd(typical)} "
                f"in estimated terms. On {len(flagged)} day(s) a user's sessions "
                f"averaged {config.session_cost_multiple:g}x that or more, "
                f"{_usd(flagged_cost)} in all. {DESCRIPTIVE}"
            ),
            measured_usd=flagged_cost,
            measured_basis=(
                "Anthropic's estimated cost of the actor-days listed, all models"
            ),
            evidence=Evidence(
                columns=("user", "day", "sessions", "est. cost", "per session", "vs median"),
                rows=rows,
                note=(
                    f"Median of {len(days)} actor-days with at least one session. "
                    "Most costly first."
                ),
            ),
            remedy=(
                "Look at what those sessions were doing. Very long sessions grow "
                "their context with every turn; starting fresh, or compacting, "
                "between unrelated tasks keeps each request smaller."
            ),
            caveats=tuple(caveats),
        )
    ]


# --------------------------------------------------------------------------
# K3 -- API vs subscription
# --------------------------------------------------------------------------


def run_customer_split(activity: CodeActivity, config: CodeConfig) -> Iterable[Finding]:
    if not activity.records:
        return []
    totals: dict[str, list] = {}
    for r in activity.records:
        entry = totals.setdefault(r.customer_type, [set(), 0, Decimal("0")])
        entry[0].add(r.actor)
        entry[1] += r.sessions
        entry[2] += r.estimated_usd
    grand = sum((t[2] for t in totals.values()), Decimal("0"))
    if grand <= 0:
        return []

    def name(kind: str) -> str:
        return {"api": "API (pay as you go)", "subscription": "subscription (Pro/Team)"}.get(
            kind, kind
        )

    ordered = sorted(totals.items(), key=lambda kv: (-kv[1][2], kv[0]))
    rows = tuple(
        (name(kind), f"{len(actors):,}", f"{sessions:,}", _usd(cost), f"{cost / grand:.0%}")
        for kind, (actors, sessions, cost) in ordered
    )
    subscription = sum(
        (cost for kind, (_a, _s, cost) in totals.items() if kind != API_CUSTOMER),
        Decimal("0"),
    )
    mix = ", ".join(f"{name(kind)} {cost / grand:.0%}" for kind, (_a, _s, cost) in ordered)
    measured = subscription if subscription >= config.min_impact_usd else None
    return [
        Finding(
            check_id="K3",
            title="Claude Code use by billing type: API vs subscription",
            confidence=Confidence.OBSERVATION,
            scope=f"{len(totals)} billing type(s)",
            summary=(
                f"Estimated cost by billing type: {mix}. API users pay per token; "
                "subscription users pay for a seat, so their figure is the API "
                f"equivalent of their use, not what they were billed. {DESCRIPTIVE}"
            ),
            measured_usd=measured,
            measured_basis=(
                "Anthropic's estimated API-equivalent cost of subscription users' "
                "Claude Code use -- not billed per token"
                if measured is not None
                else None
            ),
            evidence=Evidence(
                columns=("billing type", "users", "sessions", "est. cost", "share"),
                rows=rows,
            ),
            remedy=(
                "Compare each group's estimated cost with what it is actually "
                "paid on: a seat's price for subscribers, the API bill for the "
                "rest. Heavy subscription users can be worth more than their seat "
                "in API terms, and light API users less."
            ),
            caveats=(
                "Seat prices are not in this data, so no comparison with them is "
                "made here.",
            ),
        )
    ]


K1 = CodeCheck(id="K1", name="Tool acceptance", run=run_tool_acceptance)
K2 = CodeCheck(id="K2", name="Cost per session", run=run_session_cost)
K3 = CodeCheck(id="K3", name="API vs subscription", run=run_customer_split)
CODE_CHECKS: tuple[CodeCheck, ...] = (K1, K2, K3)


def run_code_checks(
    activity: CodeActivity,
    config: CodeConfig | None = None,
    *,
    checks: Sequence[CodeCheck] | None = None,
) -> CheckRun:
    """Run every Claude Code check. Raises ``DescriptiveOnlyViolation`` if one
    returns anything but a descriptive observation."""
    config = config or CodeConfig()
    findings: list[Finding] = []
    clean: list[str] = []
    for check in CODE_CHECKS if checks is None else checks:
        produced = list(check.run(activity, config))
        for finding in produced:
            if finding.confidence is not Confidence.OBSERVATION or finding.recoverable is not None:
                raise DescriptiveOnlyViolation(
                    f"{check.label} returned a {finding.confidence} finding. "
                    "Claude Code checks are descriptive only."
                )
        if produced:
            findings.extend(produced)
        else:
            clean.append(check.label)
    return CheckRun(findings=tuple(rank(findings)), clean=tuple(clean))
