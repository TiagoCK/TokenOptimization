"""C7r -- RBAC group coverage. `observation`

C7's question for Claude Enterprise data. That data has no workspace or API
key, so C7 cannot run; what it has instead is RBAC groups, the unit Enterprise
admins use for spend limits, product access and default models. Spend from
users in no group is spend no team can be asked to manage.

One rule shapes the arithmetic. Named-group rows overlap -- a user in two
groups contributes their whole spend to each -- so they never add up to the
account total and are never summed here. The only figure the check computes
is the null row (users in no group that day) against the ungrouped cost total,
both billed amounts read from the cost report.
"""

from __future__ import annotations

from decimal import Decimal

from ..findings import Confidence, Evidence, Finding
from ..ingest.analytics_json import GroupCoverage

CHECK_ID = "C7r"
CHECK_NAME = "RBAC group coverage"
LABEL = f"{CHECK_NAME} ({CHECK_ID})"

#: Named groups shown in the evidence table.
_MAX_GROUPS = 10


def run_group_coverage(coverage: GroupCoverage, config) -> list[Finding]:
    total = coverage.total_usd
    unassigned = coverage.unassigned_usd
    if total <= 0:
        return []
    share = unassigned / total
    if share < config.attribution_min_share or unassigned < config.min_impact_usd:
        return []

    if coverage.named:
        groups_clause = (
            f" {len(coverage.named)} named group(s) appear; their spend overlaps, "
            "because a user in several groups counts in each."
        )
    else:
        groups_clause = (
            " No named group appears at all: either no RBAC groups are set up, "
            "or none of their members had usage in this period."
        )

    ranked = sorted(coverage.named.items(), key=lambda kv: (-kv[1], kv[0]))
    rows = [
        ("in no group", f"${unassigned:,.2f}", f"{share:.0%}"),
        ("account total", f"${total:,.2f}", "100%"),
    ]
    rows += [
        (group, f"${amount:,.2f}", f"{amount / total:.0%}")
        for group, amount in ranked[:_MAX_GROUPS]
    ]

    caveats = [
        "Group membership is taken as of each UTC day, so a user added to a "
        "group mid-period counts as unassigned before that day.",
        "The unassigned row may include usage not tied to a seat user, which "
        "could never belong to a group.",
        "This is billed spend from the cost report, not a list-price estimate.",
    ]
    if coverage.capped_buckets:
        caveats.append(
            f"In {coverage.capped_buckets} day(s) the breakdown hit the API's "
            "100-group limit without an unassigned row, so the figure above is "
            "a lower bound."
        )

    return [
        Finding(
            check_id=CHECK_ID,
            title="Spend from users who belong to no RBAC group",
            confidence=Confidence.OBSERVATION,
            scope=f"{share:.0%} of billed spend",
            summary=(
                f"{share:.0%} of billed spend (${unassigned:,.2f} of "
                f"${total:,.2f}) came from users in no RBAC group.{groups_clause} "
                "This is not waste, and this check claims no savings -- but it is "
                "spend no team's limits or settings apply to."
            ),
            measured_usd=unassigned,
            measured_basis=(
                "billed spend from users in no RBAC group, from the cost report"
            ),
            evidence=Evidence(
                columns=("group", "billed spend", "share of total"),
                rows=tuple(rows),
                note=(
                    "Named groups are listed by id, as the report gives them. "
                    "Their rows overlap -- a user in two groups counts in both "
                    "-- so they do not add up to the account total."
                ),
            ),
            remedy=(
                "Assign users to RBAC groups, so spend limits, product access "
                "and default models can be set per team rather than only "
                "organisation-wide."
            ),
            caveats=tuple(caveats),
        )
    ]
