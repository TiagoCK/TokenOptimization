"""Markdown output -- the shareable artifact.

PROJECT_BRIEF.md §5: "This is what gets forwarded internally after a prospect
runs it, so it has to stand alone: period covered, total spend, pricing file
version, findings with evidence, and a caveats section listing every gap from
§2 that applied to this dataset."

*Stands alone* is the whole design constraint. This file will be read by people
who did not run the tool, cannot see the terminal it came from, and have no
reason to trust it. So it states its own provenance -- which pricing file,
fetched when, which tool version, which source files -- and it never prints a
number without saying what the number means and what it assumes.

Like the terminal renderer, this consumes `Finding[]` and knows nothing about
which checks exist. Adding a check never requires touching this file (§7).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence

from ..findings import Confidence, Evidence, Finding
from ..frame import TOKEN_FIELD_LABELS, TOKEN_FIELDS, Frame
from ..gaps import Gap, Severity
from ..pricing import PricingTable

_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}

_SEVERITY_LABEL = {
    Severity.CRITICAL: "**Critical**",
    Severity.WARNING: "Warning",
    Severity.INFO: "Note",
}


def usd(amount: Decimal) -> str:
    """Money with the sign outside the symbol: -$3.00, not $-3.00."""
    rounded = amount.quantize(Decimal("0.01"))
    sign = "-" if rounded < 0 else ""
    return f"{sign}${abs(rounded):,}"


def _escape(text: str) -> str:
    """Keep a stray pipe in a workspace name from breaking a table row."""
    return text.replace("|", "\\|")


def _table(columns: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    out = ["| " + " | ".join(_escape(c) for c in columns) + " |"]
    out.append("|" + "|".join("---" for _ in columns) + "|")
    for row in rows:
        out.append("| " + " | ".join(_escape(str(c)) for c in row) + " |")
    return out


def _anchor(finding: Finding) -> str:
    return f"#{finding.check_id.lower()}"


def render(
    frame: Frame,
    pricing: PricingTable,
    estimate,
    findings: Sequence[Finding],
    *,
    skipped: Sequence[tuple[str, str]] = (),
    version: str = "",
    generated_at: datetime | None = None,
) -> str:
    """The complete report, as one Markdown string."""
    out: list[str] = []
    now = generated_at or datetime.now(timezone.utc)

    # -- header --------------------------------------------------------
    out.append("# Claude API spend audit")
    out.append("")
    start, end = frame.period_start, frame.period_end
    period = (
        f"{start:%Y-%m-%d} to {end:%Y-%m-%d}" if start and end else "unknown period"
    )
    out.append(
        f"Covering **{period}** (UTC). Generated {now:%Y-%m-%d} by "
        f"`claude-spend-audit`{f' {version}' if version else ''}, "
        f"entirely on the machine that holds the data."
    )
    out.append("")

    # -- spend ---------------------------------------------------------
    out.append("## Spend")
    out.append("")
    billed = frame.billed_total_usd
    spend_rows = []
    if billed is not None:
        spend_rows.append(("Billed", usd(billed), "from the cost export"))
    else:
        spend_rows.append(("Billed", "not supplied", "no cost export provided"))
    spend_rows.append(
        (
            "Estimated",
            usd(estimate.usd),
            f"token counts priced with `{pricing.version}`",
        )
    )
    if billed is not None and estimate.usd:
        delta = estimate.usd - billed
        spend_rows.append(("Difference", usd(delta), "see note below"))
    out.extend(_table(("", "Amount", "Source"), spend_rows))
    out.append("")

    if billed is not None and estimate.usd:
        out.append(
            "> These two figures are deliberately not merged. A large gap is "
            "diagnostic: it can mean Priority Tier spend missing from the cost "
            "endpoint, negotiated rates the pricing file does not know about, "
            "code execution or session runtime that never appears in usage "
            "data, or a mis-mapped CSV column."
        )
        out.append("")

    if not estimate.is_complete:
        out.append(
            f"> **{estimate.unpriced_tokens:,} tokens are not priced** and are "
            "excluded from the estimate rather than counted as zero. See the "
            "caveats below for which spend this affects."
        )
        out.append("")

    # -- tokens --------------------------------------------------------
    tokens = frame.tokens
    out.append("## Tokens")
    out.append("")
    token_rows = []
    for field in TOKEN_FIELDS:
        count = getattr(tokens, field)
        share = (count / tokens.total * 100) if tokens.total else 0
        token_rows.append((TOKEN_FIELD_LABELS[field], f"{count:,}", f"{share:.1f}%"))
    token_rows.append(("**total**", f"**{tokens.total:,}**", ""))
    out.extend(_table(("Category", "Tokens", "Share"), token_rows))
    out.append("")
    if frame.web_search_requests:
        out.append(f"Plus {frame.web_search_requests:,} web search requests.")
        out.append("")

    out.extend(_findings_section(findings, _FINDINGS_INTRO))
    out.extend(_gaps_section(frame.gaps, skipped))
    out.extend(
        _provenance_section(
            sources=frame.sources,
            rates=(
                f"Every rate used here comes from `pricing/{pricing.version}.json`, "
                f"fetched {pricing.fetched_at}"
                + (f" from {pricing.source_url}" if pricing.source_url else "")
                + ". Each entry in that file records whether it was published directly "
                "or derived, and from what. If a rate is wrong for your organisation -- "
                "negotiated pricing, volume discounts -- edit that file and re-run; "
                "every figure moves with it."
            ),
        )
    )
    return "\n".join(out)


_FINDINGS_INTRO = (
    "Ranked by confidence first. Within each level, a finding that "
    "claims a saving comes before one that only measures spend, and "
    "then the larger figure first. A `measured` finding is arithmetic "
    "on your data; a `candidate` is a pattern the data cannot confirm; "
    "an `observation` makes no savings claim at all."
)


def _findings_section(findings: Sequence[Finding], intro: str) -> list[str]:
    """The ranked summary table, then a section per finding."""
    out = ["## Findings", ""]
    if not findings:
        out.append("No findings. Every check that could run found nothing to report.")
        out.append("")
        return out
    out.append(intro)
    out.append("")
    summary_rows = [
        (
            f"[{f.check_id}]({_anchor(f)})",
            f.confidence,
            usd(f.measured_usd) if f.measured_usd is not None else "-",
            str(f.recoverable) if f.recoverable else "not claimed",
            f.title,
        )
        for f in findings
    ]
    out.extend(
        _table(
            ("Check", "Confidence", "Measured", "Recoverable", "Finding"),
            summary_rows,
        )
    )
    out.append("")
    for finding in findings:
        out.extend(_render_finding(finding))
    return out


def _gaps_section(gaps: Sequence[Gap], skipped: Sequence[tuple[str, str]]) -> list[str]:
    """Every limitation that applied, then the checks that could not run."""
    out = ["## What this data cannot show", ""]
    out.append(
        "Every limitation below applied to *this* dataset. They are listed so "
        "the numbers above can be read with the right amount of confidence."
    )
    out.append("")
    if not gaps:
        out.append("Nothing recorded.")
        out.append("")
    else:
        gap_rows = [
            (
                _SEVERITY_LABEL[gap.severity],
                gap.scope or "-",
                gap.message + (f" **Fix:** {gap.remedy}" if gap.remedy else ""),
            )
            for gap in sorted(gaps, key=lambda g: _SEVERITY_ORDER[g.severity])
        ]
        out.extend(_table(("", "Scope", "Limitation"), gap_rows))
        out.append("")

    if skipped:
        out.append("### Checks that could not run")
        out.append("")
        for label, _ in skipped:
            out.append(f"- {label}")
        out.append("")
    return out


def _provenance_section(*, sources, rates: str) -> list[str]:
    """What makes the file stand alone once forwarded away from the terminal."""
    out = ["## How to check these numbers", ""]
    out.append(rates)
    out.append("")
    out.append("Source data:")
    out.append("")
    for source in sources:
        label = source.profile or source.ingest_path
        status = f", schema `{source.profile_status}`" if source.profile_status else ""
        out.append(f"- `{source.path}` (via {label}{status})")
    out.append("")
    out.append(
        "This report was produced locally. No data was uploaded, and the tool "
        "has no network access on this path."
    )
    out.append("")
    return out


_ENTERPRISE_FINDINGS_INTRO = (
    "Every finding here is an `observation`: it describes where spend went "
    "across the account and makes no savings claim. A Team/Enterprise spend "
    "report is a per-person bill summed over the export's range, and supports "
    "nothing stronger. Findings are ordered by the spend they describe, "
    "largest first."
)


def render_enterprise(
    report,
    pricing: PricingTable,
    findings: Sequence[Finding],
    *,
    skipped: Sequence[tuple[str, str]] = (),
    version: str = "",
    generated_at: datetime | None = None,
) -> str:
    """The shareable report for a Team/Enterprise spend report.

    Same contract as ``render``: it states its own provenance and never prints
    a number without saying what it is. The header differs because the data
    does -- reported spend rather than an estimate, and no time buckets.
    """
    from ..enterprise import REPORT_BANNER
    from ..enterprise.report import FIELD_LABELS, INT_FIELDS

    out: list[str] = []
    now = generated_at or datetime.now(timezone.utc)

    out.append("# Claude Team/Enterprise spend overview")
    out.append("")
    start, end = report.period_start, report.period_end
    period = (
        f"**{start:%Y-%m-%d} to {end:%Y-%m-%d}** (UTC)"
        if start and end
        else "the date range chosen when the report was exported (the file does not state it)"
    )
    out.append(
        f"Covering {period}. Generated {now:%Y-%m-%d} by "
        f"`claude-spend-audit`{f' {version}' if version else ''}, "
        f"entirely on the machine that holds the data."
    )
    out.append("")
    out.append(f"> **{REPORT_BANNER}**")
    out.append("")

    # -- spend ---------------------------------------------------------
    out.append("## Spend")
    out.append("")
    net = report.total("net_spend_usd")
    gross = report.total("gross_spend_usd")
    rows = [
        ("Net spend", usd(net) if net is not None else "not in export", "as stated in the report"),
        ("Gross spend", usd(gross) if gross is not None else "not in export", "as stated in the report"),
    ]
    if net is not None and gross:
        pct = ((gross - net) / gross * 100).quantize(Decimal("0.1"))
        rows.append(("Difference", usd(net - gross), f"{pct}% below gross"))
    out.extend(_table(("", "Amount", "Source"), rows))
    out.append("")
    out.append(
        "> These figures are read from the report, not re-priced. The pricing "
        "file is used only to group models into tiers and show list prices."
    )
    out.append("")

    # -- scope ---------------------------------------------------------
    out.append("## Coverage")
    out.append("")
    coverage = []
    for name, label in (("user_email", "Users"), ("product", "Products"), ("model", "Models")):
        coverage.append(
            (label, f"{len(report.distinct(name)):,}" if report.has(name) else "not in export")
        )
    for name in INT_FIELDS:
        value = report.total(name)
        coverage.append(
            (FIELD_LABELS[name].capitalize(), f"{value:,}" if value is not None else "not in export")
        )
    out.extend(_table(("", "Total"), coverage))
    out.append("")

    out.extend(_findings_section(findings, _ENTERPRISE_FINDINGS_INTRO))
    out.extend(_gaps_section(report.gaps, skipped))
    out.extend(
        _provenance_section(
            sources=report.sources,
            rates=(
                "Every dollar figure here is the report's own. Model tiers and "
                f"list prices come from `pricing/{pricing.version}.json`, fetched "
                f"{pricing.fetched_at}; the tiers are this tool's classification "
                "and can be edited there. Column names were matched by the "
                "`enterprise_spend` schema profile, which is provisional: run "
                "with `--explain-schema` to see what each column mapped to."
            ),
        )
    )
    return "\n".join(out)


def _render_finding(finding: Finding) -> list[str]:
    out = [f"### {finding.check_id}", ""]
    out.append(f"**{finding.title}**")
    out.append("")
    out.append(
        f"`{finding.confidence}`"
        + (f" - {finding.scope}" if finding.scope else "")
    )
    out.append("")
    out.append(finding.summary)
    out.append("")

    if finding.measured_usd is not None:
        out.append(f"**Measured: {usd(finding.measured_usd)}** -- {finding.measured_basis}")
        out.append("")

    if finding.recoverable is not None:
        out.append(f"**Recoverable: {finding.recoverable}**")
        out.append("")
        # §3: assumptions sit with the figure, never in a footnote.
        for assumption in finding.recoverable.assumptions:
            out.append(f"- {assumption}")
        out.append("")
    else:
        out.append(
            "**Recoverable: not claimed.** This figure measures what was spent, "
            "not what could be saved."
        )
        out.append("")

    if finding.evidence is not None:
        out.extend(_table(finding.evidence.columns, finding.evidence.rows))
        out.append("")
        if finding.evidence.note:
            out.append(f"*{finding.evidence.note}*")
            out.append("")

    if finding.remedy:
        out.append(f"**What to do.** {finding.remedy}")
        out.append("")

    if finding.caveats:
        out.append("**Caveats.**")
        out.append("")
        for caveat in finding.caveats:
            out.append(f"- {caveat}")
        out.append("")

    return out
