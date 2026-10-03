"""Checks for a Team/Enterprise plan spend report. All four are descriptive.

A spend report is a per-person bill: who used which model in which product,
and what it cost, summed across the export's range. That supports an
account-wide picture of where the money went. It supports no savings claim at
all -- there is no time axis, no cache split, no tier, nothing that would let
a figure be called avoidable.

So every check here is an ``observation``, and that is enforced rather than
hoped for. ``Finding`` already refuses an observation carrying a recoverable
estimate; ``run_enterprise_checks`` additionally refuses any E-check finding
that is not an observation at all, so a check added later cannot slip a
savings claim into a report people were told is descriptive.

Two of the nine usage checks carry over to this data. E3 is C8 (model mix) and
E4 is C5 (output share), reusing their helpers and keeping their restraint.
E1 and E2 are new: who the spend sits with, and in which product.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from statistics import median
from typing import Callable, Iterable, Sequence

from .. import gaps as gapmod
from ..checks import CheckRun
from ..checks._format import multiple, short_tokens
from ..checks.c5_output_share import _output_multiple
from ..checks.c8_model_mix import _ROUTING_RESEARCH, _list_price, _tier_rank
from ..findings import Confidence, Evidence, Finding, rank
from ..gaps import Gap
from ..pricing import PricingTable
from .report import SPEND, SpendReport

#: Appended to every summary, so the claim travels with the finding even when
#: it is quoted on its own.
DESCRIPTIVE = (
    "This describes where spend went; it does not claim any of it is "
    "recoverable."
)

#: Printed under the header of every spend-report output, in every format.
REPORT_BANNER = (
    "Descriptive report. Every finding below describes where spend went "
    "across the account; none claims a saving."
)

_NO_PRODUCT = "(no product recorded)"
_UNCLASSIFIED = "unclassified"

#: How a missing field is named in the gap that explains a skipped check.
_FIELD_NAMES = {
    SPEND: "net or gross spend",
    "user_email": "user email",
    "product": "product",
    "model": "model",
    "prompt_tokens": "prompt tokens",
    "completion_tokens": "completion tokens",
}


class DescriptiveOnlyViolation(ValueError):
    """An E-check produced something other than a descriptive observation."""


@dataclass(frozen=True, slots=True)
class EnterpriseConfig:
    """Thresholds for the spend-report checks."""

    #: Findings below this dollar amount are dropped rather than padding the
    #: report, as on the usage path.
    min_impact_usd: Decimal = Decimal("1")
    #: E1 reports the share of spend held by this fraction of users, rounded
    #: up to at least one person.
    top_user_fraction: Decimal = Decimal("0.10")
    #: E4 shows a segment when completion tokens are at least this share of
    #: its tokens -- the same default as C5.
    output_min_token_share: Decimal = Decimal("0.25")
    #: Rows in a per-user evidence table. The full detail is in the export.
    max_rows: int = 10


EnterpriseCheckFn = Callable[[SpendReport, PricingTable, EnterpriseConfig], Iterable[Finding]]


@dataclass(frozen=True, slots=True)
class EnterpriseCheck:
    """A registered spend-report check plus the fields it needs."""

    id: str
    name: str
    run: EnterpriseCheckFn
    #: Fields the export must carry, read through ``SpendReport.has``. The
    #: pseudo-field ``"spend"`` means a net or gross spend column.
    needs_fields: frozenset[str] = frozenset()

    @property
    def label(self) -> str:
        return f"{self.name} ({self.id})"

    def unmet(self, report: SpendReport) -> list[Gap]:
        return [
            gapmod.enterprise_field_absent(_FIELD_NAMES.get(name, name), self.label)
            for name in sorted(self.needs_fields)
            if not report.has(name)
        ]


def _usd(amount: Decimal) -> str:
    return f"${amount:,.2f}"


def _basis(report: SpendReport, what: str) -> str:
    """Every measured figure names the column it came from."""
    return f"spend observed in the report's {report.spend_label} column {what}"


def _sorted_desc(totals: dict[str, Decimal]) -> list[tuple[str, Decimal]]:
    return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))


# --------------------------------------------------------------------------
# E1 -- spend concentration by user
# --------------------------------------------------------------------------


def run_user_concentration(
    report: SpendReport, pricing: PricingTable, config: EnterpriseConfig
) -> Iterable[Finding]:
    by_user: dict[str, Decimal] = {}
    products: dict[str, set[str]] = {}
    requests: dict[str, int] = {}
    unassigned = Decimal("0")

    for row in report.rows:
        spend = report.spend_of(row)
        if row.user_email is None:
            unassigned += spend
            continue
        by_user[row.user_email] = by_user.get(row.user_email, Decimal("0")) + spend
        if row.product is not None:
            products.setdefault(row.user_email, set()).add(row.product)
        requests[row.user_email] = requests.get(row.user_email, 0) + (row.requests or 0)

    total = report.spend_total or Decimal("0")
    if total <= 0 or not by_user:
        return []

    ranked = _sorted_desc(by_user)
    users = len(ranked)
    top_count = max(
        1,
        int((Decimal(users) * config.top_user_fraction).to_integral_value(rounding=ROUND_CEILING)),
    )
    top_spend = sum((spend for _, spend in ranked[:top_count]), Decimal("0"))
    if top_spend < config.min_impact_usd:
        return []
    share = top_spend / total
    typical = median([spend for _, spend in ranked])

    def product_list(email: str) -> str:
        names = sorted(products.get(email, ()))
        if not names:
            return "-"
        shown = ", ".join(names[:3])
        return shown + (f", +{len(names) - 3}" if len(names) > 3 else "")

    columns = ["user"]
    if report.has("product"):
        columns.append("products")
    if report.has("requests"):
        columns.append("requests")
    columns += ["spend", "share"]

    rows = []
    for email, spend in ranked[: config.max_rows]:
        cells = [email]
        if report.has("product"):
            cells.append(product_list(email))
        if report.has("requests"):
            cells.append(f"{requests.get(email, 0):,}")
        cells += [_usd(spend), f"{spend / total:.0%}"]
        rows.append(tuple(cells))

    caveats = [
        "Heavy use is often the most productive use. A large share on a few "
        "people is common and is not, by itself, a sign of waste.",
        "The report sums the whole export range, so this cannot tell steady "
        "daily use from a single burst.",
    ]
    if unassigned:
        caveats.append(
            f"{_usd(unassigned)} sits on rows with no user recorded. It is in "
            "the total but in no one's share."
        )

    return [
        Finding(
            check_id="E1",
            title="How concentrated spend is across users",
            confidence=Confidence.OBSERVATION,
            scope=f"top {top_count} of {users} users",
            summary=(
                f"The top {top_count} of {users} users account for {share:.0%} "
                f"of {report.spend_label} ({_usd(top_spend)} of {_usd(total)}). "
                f"The median user's {report.spend_label} was {_usd(typical)}. "
                f"{DESCRIPTIVE}"
            ),
            measured_usd=top_spend,
            measured_basis=_basis(report, f"for the top {top_count} user(s)"),
            evidence=Evidence(
                columns=tuple(columns),
                rows=tuple(rows),
                note=(
                    f"Users ranked by {report.spend_label}, largest first; at "
                    f"most {config.max_rows} shown. Share is of the account's "
                    "total."
                ),
            ),
            remedy=(
                "Talk to the heaviest users before changing anything -- they "
                "are often the people getting the most out of it. Where usage "
                "does need bounding, Team and Enterprise plans support spend "
                "limits at the organization, group and individual level."
            ),
            caveats=tuple(caveats),
        )
    ]


# --------------------------------------------------------------------------
# E2 -- spend by product
# --------------------------------------------------------------------------


def run_product_split(
    report: SpendReport, pricing: PricingTable, config: EnterpriseConfig
) -> Iterable[Finding]:
    by_product: dict[str, Decimal] = {}
    users: dict[str, set[str]] = {}
    requests: dict[str, int] = {}

    for row in report.rows:
        product = row.product if row.product is not None else _NO_PRODUCT
        by_product[product] = by_product.get(product, Decimal("0")) + report.spend_of(row)
        if row.user_email is not None:
            users.setdefault(product, set()).add(row.user_email)
        requests[product] = requests.get(product, 0) + (row.requests or 0)

    total = report.spend_total or Decimal("0")
    if total <= 0:
        return []
    ranked = _sorted_desc(by_product)
    largest, largest_spend = ranked[0]
    if largest_spend < config.min_impact_usd:
        return []

    columns = ["product", "users"]
    if report.has("requests"):
        columns.append("requests")
    columns += ["spend", "share"]
    rows = []
    for product, spend in ranked:
        cells = [product, f"{len(users.get(product, ())):,}"]
        if report.has("requests"):
            cells.append(f"{requests.get(product, 0):,}")
        cells += [_usd(spend), f"{spend / total:.0%}"]
        rows.append(tuple(cells))

    mix = ", ".join(f"{spend / total:.0%} {product} ({_usd(spend)})" for product, spend in ranked)

    return [
        Finding(
            check_id="E2",
            title="Where spend sits across Claude products",
            confidence=Confidence.OBSERVATION,
            scope=f"{len(ranked)} product(s)",
            summary=f"{report.spend_label.capitalize()} by product: {mix}. {DESCRIPTIVE}",
            measured_usd=largest_spend,
            measured_basis=_basis(report, f"on the largest product, {largest}"),
            evidence=Evidence(
                columns=tuple(columns),
                rows=tuple(rows),
                note=(
                    "Product names are shown exactly as the export writes them; "
                    "this tool does not classify them."
                ),
            ),
            remedy=(
                "Anthropic's Enterprise consumption guide describes Claude Code "
                "and Cowork as more token-intensive than chat. Product access "
                "can be assigned per group, so a product's share can be steered "
                "by who has access to it, not only by how it is used."
            ),
            caveats=(
                "A high share on Claude Code is expected where engineers are the "
                "main users. It is context, not a verdict.",
                "The report sums the whole export range, so this cannot show "
                "whether the split is stable or shifting.",
            ),
        )
    ]


# --------------------------------------------------------------------------
# E3 -- model mix (C8 on spend-report data)
# --------------------------------------------------------------------------


def run_model_mix(
    report: SpendReport, pricing: PricingTable, config: EnterpriseConfig
) -> Iterable[Finding]:
    spend: dict[str, Decimal] = {}
    for row in report.rows:
        if row.model is None:
            continue
        model = pricing.resolve_model(row.model) or row.model
        spend[model] = spend.get(model, Decimal("0")) + report.spend_of(row)

    total = sum(spend.values(), Decimal("0"))
    if total <= 0:
        return []

    tier_of = {model: pricing.tier_of(model) or _UNCLASSIFIED for model in spend}
    by_tier: dict[str, Decimal] = {}
    for model, usd in spend.items():
        by_tier[tier_of[model]] = by_tier.get(tier_of[model], Decimal("0")) + usd

    frontier = by_tier.get("frontier", Decimal("0"))
    if frontier < config.min_impact_usd:
        return []  # nothing on the most expensive tier worth a line, as in C8
    frontier_share = frontier / total

    ordered_tiers = sorted(by_tier, key=_tier_rank)
    mix = ", ".join(f"{by_tier[t] / total:.0%} {t} ({_usd(by_tier[t])})" for t in ordered_tiers)

    models = sorted(spend, key=lambda m: (_tier_rank(tier_of[m]), -spend[m], m))
    rows = tuple(
        (
            model,
            tier_of[model],
            _usd(spend[model]),
            f"{spend[model] / total:.0%}",
            _list_price(pricing, model),
        )
        for model in models
    )

    caveats = [
        "This check cannot tell whether any request needed the model it used. "
        "It reports the mix; sizing a change takes per-request evaluation, which "
        "a spend report cannot support.",
        _ROUTING_RESEARCH,
        "List prices are the API rate card in the pricing file. Plan pricing may "
        "differ; the spend column is what the report says was spent.",
    ]
    if _UNCLASSIFIED in by_tier:
        caveats.append(
            f"{_usd(by_tier[_UNCLASSIFIED])} is on models the pricing file does "
            "not recognise. It is shown as unclassified rather than left out, so "
            "the shares still add up to the total."
        )

    return [
        Finding(
            check_id="E3",
            title="Model mix: where spend sits across frontier, mid and small models",
            confidence=Confidence.OBSERVATION,
            scope=f"{frontier_share:.0%} of {report.spend_label} on frontier models",
            summary=(
                f"{report.spend_label.capitalize()} by tier: {mix}. This check "
                "makes no routing claim: whether a request needed a frontier "
                f"model is a property of the request, which this data never "
                f"sees. {DESCRIPTIVE}"
            ),
            measured_usd=frontier,
            measured_basis=_basis(
                report, "on models the pricing file classifies as frontier"
            ),
            evidence=Evidence(
                columns=("model", "tier", "spend", "share", "list $/MTok in / out"),
                rows=rows,
                note=(
                    "Tiers are this tool's classification, recorded in the "
                    "pricing file -- not Anthropic's."
                ),
            ),
            remedy=(
                "If some usage is simple -- quick questions, short rewrites -- a "
                "smaller default model may serve it. Admins can set the "
                "organization's default model and restrict which models a group "
                "can use. Try a change on one group first; this data cannot say "
                "which requests needed a frontier model."
            ),
            caveats=tuple(caveats),
        )
    ]


# --------------------------------------------------------------------------
# E4 -- output token share (C5 on spend-report data)
# --------------------------------------------------------------------------


def run_output_share(
    report: SpendReport, pricing: PricingTable, config: EnterpriseConfig
) -> Iterable[Finding]:
    prompt: dict[str, int] = {}
    completion: dict[str, int] = {}
    spend: dict[str, Decimal] = {}
    models: dict[str, set[str]] = {}
    has_spend = report.has(SPEND)

    for row in report.rows:
        model = (pricing.resolve_model(row.model) or row.model) if row.model else "(no model)"
        label = f"{row.product} / {model}" if row.product is not None else model
        prompt[label] = prompt.get(label, 0) + (row.prompt_tokens or 0)
        completion[label] = completion.get(label, 0) + (row.completion_tokens or 0)
        if has_spend:
            spend[label] = spend.get(label, Decimal("0")) + report.spend_of(row)
        models.setdefault(label, set()).add(model)

    threshold = config.output_min_token_share
    flagged = []
    for label in prompt:
        tokens = prompt[label] + completion[label]
        if tokens == 0:
            continue
        if Decimal(completion[label]) / Decimal(tokens) < threshold:
            continue
        if has_spend and spend[label] < config.min_impact_usd:
            continue
        flagged.append(label)

    if not flagged:
        return []
    flagged.sort(key=lambda l: (-(spend.get(l) or Decimal("0")), -completion[l], l))

    flagged_prompt = sum(prompt[l] for l in flagged)
    flagged_completion = sum(completion[l] for l in flagged)
    token_share = Decimal(flagged_completion) / Decimal(flagged_prompt + flagged_completion)
    flagged_spend = sum((spend[l] for l in flagged), Decimal("0")) if has_spend else None

    involved = {model for label in flagged for model in models[label]}
    multiples = sorted(
        {m for model in involved if (m := _output_multiple(pricing, model)) is not None}
    )
    if not multiples:
        price_clause = ""
    else:
        costs_what = (
            multiple(multiples[0])
            if len(multiples) == 1
            else f"between {multiple(multiples[0])} and {multiple(multiples[-1])}"
        )
        price_clause = (
            f" On the API rate card, output costs {costs_what} as much as "
            "uncached input on these models."
        )

    columns = ["product / model" if report.has("product") else "model", "prompt", "completion", "of tokens"]
    if has_spend:
        columns.append("spend")
    rows = []
    for label in flagged:
        cells = [
            label,
            short_tokens(prompt[label]),
            short_tokens(completion[label]),
            f"{Decimal(completion[label]) / Decimal(prompt[label] + completion[label]):.0%}",
        ]
        if has_spend:
            cells.append(_usd(spend[label]))
        rows.append(tuple(cells))

    spend_clause = (
        f" These segments' {report.spend_label} was {_usd(flagged_spend)} across "
        "all token types; the report does not split spend by token type."
        if flagged_spend is not None
        else ""
    )
    caveats = [
        "Completion tokens include thinking, which is billed as output. The "
        "report does not separate the two, so a high share may be reasoning "
        "rather than long answers.",
        _prompt_caveat(report),
        "There is one row per person per model, not per request, so this cannot "
        "tell a few long responses from many short ones.",
        "Some work is legitimately generative: drafting, code generation and "
        "translation write more than they read.",
    ]

    return [
        Finding(
            check_id="E4",
            title="Segments where completion tokens are a large share of tokens",
            confidence=Confidence.OBSERVATION,
            scope=f"{len(flagged)} segment(s)",
            summary=(
                f"In {len(flagged)} segment(s), completion tokens are "
                f"{token_share:.0%} of tokens.{price_clause}{spend_clause} "
                f"{DESCRIPTIVE}"
            ),
            measured_usd=flagged_spend,
            measured_basis=(
                _basis(report, "for the segments shown, across all token types")
                if flagged_spend is not None
                else None
            ),
            evidence=Evidence(
                columns=tuple(columns),
                rows=tuple(rows),
                note=(
                    f"Shown because completion tokens are at least {threshold:.0%} "
                    "of the segment's prompt plus completion tokens."
                ),
            ),
            remedy=(
                "Enterprise lets users cap the effort level -- how much thinking "
                "Claude applies -- and thinking is billed as output. For groups "
                "doing quick lookups a lower default effort may fit; for drafting "
                "or code generation, a high output share may simply be the job."
            ),
            caveats=tuple(caveats),
        )
    ]


def _prompt_caveat(report: SpendReport) -> str:
    """What "prompt tokens" means here, as far as the source says."""
    if report.prompt_includes_cache:
        return (
            "Prompt tokens here include cache reads and cache writes as well as "
            "uncached input, so the share compares output with everything the "
            "model read."
        )
    return (
        "The export does not document whether prompt tokens include cache reads "
        "and writes. If they exclude them, the share shown overstates output "
        "relative to everything the model read."
    )


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

E1 = EnterpriseCheck(
    id="E1",
    name="Spend concentration by user",
    run=run_user_concentration,
    needs_fields=frozenset({"user_email", SPEND}),
)
E2 = EnterpriseCheck(
    id="E2",
    name="Spend by product",
    run=run_product_split,
    needs_fields=frozenset({"product", SPEND}),
)
E3 = EnterpriseCheck(
    id="E3",
    name="Model mix",
    run=run_model_mix,
    needs_fields=frozenset({"model", SPEND}),
)
E4 = EnterpriseCheck(
    id="E4",
    name="Output token share",
    run=run_output_share,
    needs_fields=frozenset({"prompt_tokens", "completion_tokens"}),
)

ENTERPRISE_CHECKS: tuple[EnterpriseCheck, ...] = (E1, E2, E3, E4)


def _model_gaps(report: SpendReport, pricing: PricingTable) -> list[Gap]:
    """Models the pricing file cannot classify. Informational only."""
    return [
        gapmod.enterprise_model_unclassified(model)
        for model in report.distinct("model")
        if pricing.resolve_model(model) is None
    ]


def run_enterprise_checks(
    report: SpendReport,
    pricing: PricingTable,
    config: EnterpriseConfig | None = None,
    *,
    checks: Sequence[EnterpriseCheck] | None = None,
) -> CheckRun:
    """Run every spend-report check the export can support.

    Raises ``DescriptiveOnlyViolation`` if a check returns anything but a
    descriptive observation. That is a bug in the check, and failing loudly
    beats shipping a savings claim in a report labelled descriptive.
    """
    config = config or EnterpriseConfig()
    registry = list(ENTERPRISE_CHECKS if checks is None else checks)

    findings: list[Finding] = []
    gaps: list[Gap] = []
    clean: list[str] = []
    skipped: list[tuple[str, str]] = []

    for check in registry:
        blocking = check.unmet(report)
        if blocking:
            gaps.extend(blocking)
            skipped.append((check.label, "; ".join(g.message for g in blocking)))
            continue

        produced = list(check.run(report, pricing, config))
        for finding in produced:
            if finding.confidence is not Confidence.OBSERVATION or finding.recoverable is not None:
                raise DescriptiveOnlyViolation(
                    f"{check.label} returned a {finding.confidence} finding"
                    + (" with a recoverable estimate" if finding.recoverable else "")
                    + ". Spend-report checks are descriptive only: every finding "
                    "must be an observation with no savings claim."
                )
        if produced:
            findings.extend(produced)
        else:
            clean.append(check.label)

    if report.has("model"):
        gaps.extend(_model_gaps(report, pricing))

    return CheckRun(
        findings=tuple(rank(findings)),
        gaps=tuple(gaps),
        clean=tuple(clean),
        skipped=tuple(skipped),
    )
