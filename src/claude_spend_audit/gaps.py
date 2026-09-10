"""Known gaps in the source data, modelled as data rather than as print calls.

PROJECT_BRIEF.md §2 lists constraints that are "not edge cases, they're
load-bearing". §5 requires the Markdown report to carry a caveats section
listing every gap that applied to the dataset in hand.

Renderers iterate ``Frame.gaps`` generically, so adding a gap never requires
touching a renderer -- the same rule §7 sets for checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    #: The headline total is wrong and must not be presented as the bill.
    CRITICAL = "critical"
    #: Some analysis is unavailable or some spend is unpriced, but the
    #: figures that are shown are sound.
    WARNING = "warning"
    #: Context for the caveats section. No impact on correctness.
    INFO = "info"


class GapCode(StrEnum):
    # Costs this data source cannot see or price
    PRIORITY_TIER_PRESENT = "priority_tier_present"
    UNPRICED_SERVICE_TIER = "unpriced_service_tier"
    UNKNOWN_MODEL = "unknown_model"
    CODE_EXECUTION_COST_ONLY = "code_execution_cost_only"
    SESSION_USAGE_COST_ONLY = "session_usage_cost_only"

    # Routes whose data lives somewhere else entirely
    BEDROCK_NOT_VISIBLE = "bedrock_not_visible"
    ENTERPRISE_ANALYTICS_SOURCE = "enterprise_analytics_source"

    # Shape of the export limits what can be concluded
    DIMENSION_NOT_GROUPED = "dimension_not_grouped"
    GRANULARITY_TOO_COARSE = "granularity_too_coarse"
    NO_COST_DATA = "no_cost_data"
    NO_USAGE_DATA = "no_usage_data"

    # Findings about the data itself
    UNATTRIBUTED_SPEND = "unattributed_spend"
    PROVISIONAL_CSV_SCHEMA = "provisional_csv_schema"
    UNMAPPED_CSV_COLUMNS = "unmapped_csv_columns"


@dataclass(frozen=True, slots=True)
class Gap:
    """One known limitation, scoped to what it actually affects."""

    code: GapCode
    severity: Severity
    message: str
    #: What the gap applies to -- a model ID, a tier, a dimension name, or ""
    #: for whole-dataset gaps. Part of the dedupe key in Frame.with_gaps.
    scope: str = ""
    #: True when this gap means a headline dollar total understates the real
    #: bill. §2: "surface a loud warning rather than silently reporting a
    #: wrong total."
    invalidates_total: bool = False
    #: What the user can do about it, when there is something.
    remedy: str | None = None

    def __str__(self) -> str:
        prefix = f"[{self.severity.upper()}]"
        scope = f" ({self.scope})" if self.scope else ""
        return f"{prefix}{scope} {self.message}"


# --------------------------------------------------------------------------
# Constructors. Kept here so wording stays consistent everywhere a gap is
# raised, and so the §2 constraints read as one list.
# --------------------------------------------------------------------------


def priority_tier_present(tier: str) -> Gap:
    return Gap(
        code=GapCode.PRIORITY_TIER_PRESENT,
        severity=Severity.CRITICAL,
        scope=tier,
        message=(
            f"Service tier {tier!r} is present in the usage data. Priority Tier "
            "uses a different billing model and its costs are absent from the "
            "cost endpoint entirely, so any billed total below understates the "
            "real bill by an unknown amount."
        ),
        invalidates_total=True,
        remedy=(
            "Reconcile against the invoice before quoting a total. The token "
            "counts for this tier are still accurate."
        ),
    )


def unpriced_service_tier(tier: str, reason: str) -> Gap:
    return Gap(
        code=GapCode.UNPRICED_SERVICE_TIER,
        severity=Severity.WARNING,
        scope=tier,
        message=(
            f"No published rate for service tier {tier!r}, so its spend is "
            f"excluded from estimated cost rather than guessed. {reason}"
        ),
        invalidates_total=True,
        remedy=(
            f"If you have negotiated rates for {tier!r}, add them to the "
            "pricing file and re-run."
        ),
    )


def unknown_model(model: str) -> Gap:
    return Gap(
        code=GapCode.UNKNOWN_MODEL,
        severity=Severity.WARNING,
        scope=model,
        message=(
            f"Model {model!r} is not in the pricing file, so its tokens are "
            "counted but not priced."
        ),
        invalidates_total=True,
        remedy=f"Add a {model!r} entry to the pricing file and re-run.",
    )


def code_execution_cost_only() -> Gap:
    return Gap(
        code=GapCode.CODE_EXECUTION_COST_ONLY,
        severity=Severity.INFO,
        message=(
            "Code execution spend is billed by container time, not tokens. It "
            "appears only in the cost data and never in usage, so it cannot be "
            "attributed to a model or broken down further here."
        ),
    )


def session_usage_cost_only() -> Gap:
    return Gap(
        code=GapCode.SESSION_USAGE_COST_ONLY,
        severity=Severity.INFO,
        message=(
            "Managed Agents session runtime is billed per session-hour. It "
            "appears only in the cost data and never in usage."
        ),
    )


def bedrock_not_visible(detected: bool = False) -> Gap:
    if detected:
        message = (
            "A Bedrock marker appears in this data. Claude Platform on AWS has "
            "no programmatic Usage/Cost API, so Bedrock-routed traffic is not "
            "represented here and totals cover first-party API traffic only."
        )
        severity = Severity.CRITICAL
    else:
        message = (
            "This data source covers first-party API traffic only. If any "
            "workload routes through Amazon Bedrock, its spend is not visible "
            "here and no signal in aggregate data would reveal it."
        )
        severity = Severity.INFO
    return Gap(
        code=GapCode.BEDROCK_NOT_VISIBLE,
        severity=severity,
        message=message,
        invalidates_total=detected,
        remedy="Pull Bedrock spend from AWS Cost Explorer and add it separately.",
    )


def dimension_not_grouped(dimension: str, blocks: str) -> Gap:
    return Gap(
        code=GapCode.DIMENSION_NOT_GROUPED,
        severity=Severity.WARNING,
        scope=dimension,
        message=(
            f"This export is not broken out by {dimension!r}, so {blocks} "
            "cannot be evaluated. This is a property of the export, not a "
            "finding about the spend."
        ),
        remedy=(
            f"Re-export from the Console grouped by {dimension!r}, or use the "
            "Admin API path with that dimension in group_by."
        ),
    )


def granularity_too_coarse(have: str, need: str, blocks: str) -> Gap:
    return Gap(
        code=GapCode.GRANULARITY_TOO_COARSE,
        severity=Severity.WARNING,
        scope=have,
        message=(
            f"Data is bucketed at {have!r}; {blocks} needs {need!r} buckets to "
            "distinguish sustained overnight load from ordinary daytime volume."
        ),
        remedy=f"Re-export at {need!r} granularity.",
    )


def no_cost_data() -> Gap:
    return Gap(
        code=GapCode.NO_COST_DATA,
        severity=Severity.INFO,
        message=(
            "No cost data supplied, so every dollar figure is estimated from "
            "token counts and the pricing file rather than read from billing."
        ),
        remedy="Add a Cost page export with --cost to compare against billing.",
    )


def no_usage_data() -> Gap:
    return Gap(
        code=GapCode.NO_USAGE_DATA,
        severity=Severity.WARNING,
        message=(
            "No usage data supplied. Cost totals are available but no "
            "token-level analysis is possible."
        ),
    )


def unattributed_spend(dimension: str, share: str) -> Gap:
    return Gap(
        code=GapCode.UNATTRIBUTED_SPEND,
        severity=Severity.WARNING,
        scope=dimension,
        message=(
            f"{share} of spend has no {dimension!r} attached. For workspaces "
            "this is the default workspace; for API keys it is Console "
            "playground traffic."
        ),
    )


def provisional_csv_schema(profile: str) -> Gap:
    return Gap(
        code=GapCode.PROVISIONAL_CSV_SCHEMA,
        severity=Severity.WARNING,
        scope=profile,
        message=(
            f"CSV schema profile {profile!r} is marked provisional: Anthropic "
            "does not publish the Console export's column headers, so the "
            "header mapping has not been verified against a real export."
        ),
        remedy=(
            "Run with --explain-schema to see exactly which column mapped to "
            "which field, and correct the profile JSON if anything is wrong."
        ),
    )


def unmapped_csv_columns(columns: tuple[str, ...]) -> Gap:
    listed = ", ".join(repr(c) for c in columns)
    return Gap(
        code=GapCode.UNMAPPED_CSV_COLUMNS,
        severity=Severity.WARNING,
        scope=",".join(columns),
        message=(
            f"{len(columns)} column(s) in the export were not recognised and "
            f"have been ignored: {listed}."
        ),
        remedy=(
            "If any of these carry data the analysis needs, add them to the "
            "schema profile's aliases."
        ),
    )
