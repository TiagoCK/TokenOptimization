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

    # A Team/Enterprise spend report lacks something a check needs
    ENTERPRISE_FIELD_ABSENT = "enterprise_field_absent"
    ENTERPRISE_MODEL_UNCLASSIFIED = "enterprise_model_unclassified"
    ENTERPRISE_ROWS_EXCLUDED = "enterprise_rows_excluded"

    # Claude Enterprise Analytics API
    ANALYTICS_GROUPS_TRUNCATED = "analytics_groups_truncated"
    ANALYTICS_INCOMPLETE_TAIL = "analytics_incomplete_tail"
    ANALYTICS_NOT_FINAL = "analytics_not_final"
    ANALYTICS_PLAN_CONTEXT = "analytics_plan_context"
    LIST_PRICE_BASIS = "list_price_basis"

    # Shape of the export limits what can be concluded
    DIMENSION_NOT_GROUPED = "dimension_not_grouped"
    GRANULARITY_TOO_COARSE = "granularity_too_coarse"
    INSUFFICIENT_HISTORY = "insufficient_history"
    NO_COST_DATA = "no_cost_data"
    NO_USAGE_DATA = "no_usage_data"

    # The source threw away detail the API provides
    CACHE_TTL_NOT_SPLIT = "cache_ttl_not_split"
    MODEL_ALIAS_MERGED = "model_aliases_merged"

    # A rate was applied that the pricing page does not confirm
    LONG_CONTEXT_RATE_UNCONFIRMED = "long_context_rate_unconfirmed"
    PRICING_DIMENSION_NOT_GROUPED = "pricing_dimension_not_grouped"

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


def insufficient_history(have: int, need: int, blocks: str) -> Gap:
    return Gap(
        code=GapCode.INSUFFICIENT_HISTORY,
        severity=Severity.WARNING,
        scope=f"{have} day(s)",
        message=(
            f"The data covers {have} day(s); {blocks} needs at least {need} -- "
            "a baseline spanning a full week, weekend included, plus a day to "
            "compare against it. Run on less, it could only report nothing, "
            "and nothing would read as a clean result."
        ),
        remedy="Export a longer range; a month is typical.",
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


def cache_ttl_not_split(tokens: int) -> Gap:
    """The source summed the two cache-creation TTLs.

    Anthropic's own cookbook exporter does this. It is unrecoverable, and it
    is the one distinction C1 depends on: a 5m write costs 1.25x base input
    and a 1h write 2x, so break-even differs fourfold between them.

    The tokens are still counted and still priced -- at the 5m rate, which is
    the lower of the two -- so the estimate is a floor rather than a guess.
    """
    return Gap(
        code=GapCode.CACHE_TTL_NOT_SPLIT,
        severity=Severity.WARNING,
        scope=f"{tokens:,} tokens",
        message=(
            f"{tokens:,} cache-write tokens arrived with their TTL summed away. "
            "The two cache TTLs are priced differently (1.25x base input for "
            "5 minutes, 2x for 1 hour), so these are priced at the cheaper 5m "
            "rate and the spend figure is a floor -- up to 60% more if they "
            "were all 1-hour writes. Cache write amortization (C1) cannot run "
            "on them at all, because the whole check is the difference between "
            "the two."
        ),
        invalidates_total=True,
        remedy=(
            "Export from the Usage & Cost API rather than a tool that sums "
            "cache_creation. The API reports ephemeral_5m_input_tokens and "
            "ephemeral_1h_input_tokens separately; Anthropic's cookbook "
            "exporter adds them together before writing its CSV."
        ),
    )


def model_aliases_merged(canonical: str, spellings: tuple[str, ...]) -> Gap:
    """Several spellings of one model, combined into its canonical id.

    Left alone they would split one model's spend across segments and every
    downstream share would be computed on a fraction of it. Merging is a
    rewrite of the source data, so it is reported here rather than done
    quietly -- the reader can see exactly which strings were treated as the
    same thing and disagree if they know better.
    """
    listed = ", ".join(repr(s) for s in spellings)
    return Gap(
        code=GapCode.MODEL_ALIAS_MERGED,
        severity=Severity.INFO,
        scope=canonical,
        message=(
            f"{len(spellings)} spellings of {canonical!r} appeared in this data "
            f"({listed}) and have been combined under the canonical id. Without "
            "that, this model's spend would be split across separate rows and "
            "every share and ranking involving it would be computed on the "
            "parts rather than the whole."
        ),
        remedy=(
            "Check the export for trailing whitespace or mixed identifier "
            "styles. The figures above are correct either way; consistent "
            "identifiers just make the raw export easier to work with."
        ),
    )


def long_context_rate_unconfirmed(model: str, tokens: int) -> Gap:
    """Long-context traffic on a model the pricing page is silent about.

    The page confirms standard-rate 1M context for Claude 4.6 and later only.
    Earlier models that offered a 1M window did so as a beta, and the page no
    longer says what that costs. Pricing it at the standard rate is the only
    figure the file supports, but presenting it without comment would be
    claiming a confirmation that does not exist.

    Not ``invalidates_total``: that flag drives the exit code and means the
    total is *known* to understate. Here it may or may not; the gap exists so
    the reader can find out.
    """
    return Gap(
        code=GapCode.LONG_CONTEXT_RATE_UNCONFIRMED,
        severity=Severity.WARNING,
        scope=model,
        message=(
            f"{tokens:,} tokens on {model!r} fall in the 200k-1M context window. "
            "The pricing page confirms standard-rate long context for Claude "
            "4.6 and later only, and says nothing about this model, so these "
            "tokens are priced at the standard rate without confirmation. If a "
            "long-context premium applies, the estimate understates them."
        ),
        remedy=(
            "Check the invoice for this model's long-context lines, and add a "
            "rate to the pricing file if they differ."
        ),
    )


def pricing_dimension_not_grouped(dimensions: tuple[str, ...]) -> Gap:
    """The export omits a dimension the rate depends on.

    ``service_tier``, ``inference_geo`` and ``speed`` all change the price of a
    token: batch is half price, US-only inference 1.1x, fast mode 2x. With the
    dimension absent every row is priced at the baseline, and nothing in the
    numbers would show it.
    """
    assumed = {
        "service_tier": "standard tier (batch would be overstated, priority priced when it should not be)",
        "inference_geo": "global routing (US-only inference, at 1.1x, would be understated)",
        "speed": "standard speed (fast mode, at 2x, would be understated)",
    }
    listed = "; ".join(f"{d}: assumed {assumed.get(d, 'baseline')}" for d in dimensions)
    return Gap(
        code=GapCode.PRICING_DIMENSION_NOT_GROUPED,
        severity=Severity.WARNING,
        scope=", ".join(dimensions),
        message=(
            f"The usage data is not broken out by {', '.join(dimensions)}, each "
            "of which changes the price of a token. The estimate therefore "
            f"prices every token at the baseline -- {listed}."
        ),
        remedy=(
            "Re-export with these dimensions included, or fetch with them in "
            "--group-by (the default includes all three)."
        ),
    )


def provisional_csv_schema(
    profile: str, unpublished: str = "the Console export's column headers"
) -> Gap:
    return Gap(
        code=GapCode.PROVISIONAL_CSV_SCHEMA,
        severity=Severity.WARNING,
        scope=profile,
        message=(
            f"CSV schema profile {profile!r} is marked provisional: Anthropic "
            f"does not publish {unpublished}, so the header mapping has not "
            "been verified against a real export."
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


# --------------------------------------------------------------------------
# Team / Enterprise spend report. None of these set invalidates_total: the
# spend in that report is Anthropic's own figure, read rather than estimated,
# so nothing here means a total is known to understate the bill.
# --------------------------------------------------------------------------


def enterprise_analytics_source() -> Gap:
    """What a Team/Enterprise spend report is, and what it cannot show."""
    return Gap(
        code=GapCode.ENTERPRISE_ANALYTICS_SOURCE,
        severity=Severity.INFO,
        message=(
            "This is a Team/Enterprise plan spend report. It covers seat-plan "
            "usage -- chat, Claude Code and Cowork -- and not Developer "
            "Platform API traffic, which has its own Usage and Cost exports. "
            "Each row is one person's usage of one model summed across the "
            "export's whole date range, so there is no daily or hourly "
            "breakdown: spikes, off-hours volume, caching efficiency, batch "
            "eligibility and context length cannot be analysed from it. Spend "
            "is the report's own figure, not re-priced by this tool."
        ),
        remedy=(
            "For API traffic, run this tool on a Developer Console export or "
            "an Admin API fetch instead."
        ),
    )


def enterprise_field_absent(field: str, blocks: str) -> Gap:
    """A column a check needs is not in the spend report as matched."""
    return Gap(
        code=GapCode.ENTERPRISE_FIELD_ABSENT,
        severity=Severity.WARNING,
        scope=field,
        message=(
            f"No {field!r} column was recognised in this spend report, so "
            f"{blocks} cannot be evaluated. This is a property of the export "
            "(or of the column mapping), not a finding about the spend."
        ),
        remedy=(
            "If the export does carry this column under another name, add that "
            "header to the field's aliases in schemas/enterprise_spend_csv.json "
            "and re-run. --explain-schema shows what each column mapped to."
        ),
    )


def enterprise_model_unclassified(model: str) -> Gap:
    """A model in the spend report that the pricing file cannot resolve.

    Unlike ``unknown_model``, this does not invalidate a total: the report's
    spend is already in dollars. All that is lost is the model's tier and list
    price in the model-mix observation.
    """
    return Gap(
        code=GapCode.ENTERPRISE_MODEL_UNCLASSIFIED,
        severity=Severity.INFO,
        scope=model,
        message=(
            f"Model {model!r} is not in the pricing file, so the model-mix "
            "observation shows it as unclassified and without a list price. "
            "Its spend is still counted: the report supplies it in dollars."
        ),
        remedy=(
            f"Add a {model!r} entry (or an alias) to the pricing file to "
            "classify it."
        ),
    )


def enterprise_rows_excluded(count: int) -> Gap:
    """Rows with neither a user nor a model, left out of every figure.

    The one place this path drops rows, and the reason it is reported rather
    than done quietly. A row naming no person and no model is most likely a
    totals line a spreadsheet added; counting it would double the spend.
    """
    return Gap(
        code=GapCode.ENTERPRISE_ROWS_EXCLUDED,
        severity=Severity.WARNING,
        scope=f"{count} row(s)",
        message=(
            f"{count} row(s) in the spend report name neither a user nor a "
            "model and were left out of every figure. Rows like that are "
            "usually a totals line, which would double the spend if counted."
        ),
        remedy=(
            "Check the export for a summary row. If these rows carry real "
            "usage, the user or model column is probably mis-mapped: run with "
            "--explain-schema."
        ),
    )


# --------------------------------------------------------------------------
# Claude Enterprise Analytics API
# --------------------------------------------------------------------------


def analytics_groups_truncated(missing_tokens: int, share: str, buckets: int) -> Gap:
    """Grouped buckets that came back with less than the bucket's total.

    The API reports at most the top 100 groups per bucket and has no
    remainder row, so a grouped fetch can silently drop the tail. The tool
    fetches an ungrouped total beside it and compares, bucket by bucket.
    """
    return Gap(
        code=GapCode.ANALYTICS_GROUPS_TRUNCATED,
        severity=Severity.WARNING,
        scope=f"{buckets} bucket(s)",
        message=(
            f"{missing_tokens:,} tokens ({share} of the total) are missing from "
            f"the grouped data in {buckets} time bucket(s). The Analytics API "
            "returns at most the top 100 groups per bucket, with no remainder "
            "row, so every estimate and check below is computed on less than "
            "the whole."
        ),
        invalidates_total=True,
        remedy=(
            "Fetch again with fewer --group-by dimensions, so each bucket has "
            "fewer than 100 distinct combinations."
        ),
    )


def analytics_incomplete_tail(refreshed_at: str | None) -> Gap:
    """The requested range runs past the export the API served it from."""
    if refreshed_at is None:
        message = (
            "The Analytics API had no export covering part of the requested "
            "range when this was fetched, so that part is empty rather than "
            "zero."
        )
    else:
        message = (
            f"The requested range runs past {refreshed_at}, the point the "
            "Analytics API's data had been refreshed to when this was fetched. "
            "Anything after it is incomplete, so totals for the end of the "
            "range understate the usage."
        )
    return Gap(
        code=GapCode.ANALYTICS_INCOMPLETE_TAIL,
        severity=Severity.WARNING,
        scope=refreshed_at or "",
        message=message,
        invalidates_total=True,
        remedy=(
            "Fetch again with --to at or before the refresh time, or wait a few "
            "hours: data is typically refreshed every 4 hours."
        ),
    )


def analytics_not_final(days: int) -> Gap:
    return Gap(
        code=GapCode.ANALYTICS_NOT_FINAL,
        severity=Severity.INFO,
        scope=f"{days} day(s)",
        message=(
            f"{days} day(s) of this range fall within the last 30 days. The "
            "Analytics API revises figures for up to 30 days as late events "
            "arrive and reconciliation runs, so these may still change."
        ),
        remedy="For invoicing-grade totals, fetch dates at least 30 days old.",
    )


def list_price_basis(ratio: str) -> Gap:
    """Check figures are at list price; the account pays something else."""
    return Gap(
        code=GapCode.LIST_PRICE_BASIS,
        severity=Severity.INFO,
        scope=ratio,
        message=(
            "Dollar figures in the checks price tokens at the list rates in "
            f"the pricing file. This account's billed amount is {ratio} of its "
            "list-price amount over the period, so actual spend on any finding "
            "is likely lower by roughly that ratio. It is not applied to "
            "individual findings, because the discount need not be uniform."
        ),
    )


def analytics_plan_context() -> Gap:
    """What Claude Enterprise data is, and what it leaves out."""
    return Gap(
        code=GapCode.ANALYTICS_PLAN_CONTEXT,
        severity=Severity.INFO,
        message=(
            "This is Claude Enterprise plan usage from the Analytics API: "
            "chat, Claude Code, Cowork and other Claude products. Plan products "
            "manage prompt caching themselves, so remedies that change cache "
            "configuration may not be available, though the cache figures are "
            "still measured. The data carries no service tier, workspace or "
            "API key, so batch adoption (C3) and attribution coverage (C7) "
            "cannot run on it -- batch pricing does not apply to plan usage "
            "in any case. On seat-based plans "
            "cost reflects usage credits only. Claude Code routed through "
            "Amazon Bedrock is not included, and per-user figures cover seat "
            "users only, not direct API-key or automation traffic."
        ),
    )
