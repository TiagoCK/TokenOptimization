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

    # -- findings ------------------------------------------------------
    out.append("## Findings")
    out.append("")
    if not findings:
        out.append("No findings. Every check that could run found nothing to report.")
        out.append("")
    else:
        out.append(
            "Ranked by confidence first, then by size. A `measured` finding is "
            "arithmetic on your data; a `candidate` is a pattern the data cannot "
            "confirm; an `observation` makes no savings claim at all."
        )
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

    # -- what the data cannot show --------------------------------------
    out.append("## What this data cannot show")
    out.append("")
    out.append(
        "Every limitation below applied to *this* dataset. They are listed so "
        "the numbers above can be read with the right amount of confidence."
    )
    out.append("")
    if not frame.gaps:
        out.append("Nothing recorded.")
        out.append("")
    else:
        gap_rows = [
            (
                _SEVERITY_LABEL[gap.severity],
                gap.scope or "-",
                gap.message + (f" **Fix:** {gap.remedy}" if gap.remedy else ""),
            )
            for gap in sorted(frame.gaps, key=lambda g: _SEVERITY_ORDER[g.severity])
        ]
        out.extend(_table(("", "Scope", "Limitation"), gap_rows))
        out.append("")

    if skipped:
        out.append("### Checks that could not run")
        out.append("")
        for label, _ in skipped:
            out.append(f"- {label}")
        out.append("")

    # -- provenance ------------------------------------------------------
    # This is what makes the file stand alone once it has been forwarded away
    # from the terminal that produced it.
    out.append("## How to check these numbers")
    out.append("")
    out.append(
        f"Every rate used here comes from `pricing/{pricing.version}.json`, "
        f"fetched {pricing.fetched_at}"
        + (f" from {pricing.source_url}" if pricing.source_url else "")
        + ". Each entry in that file records whether it was published directly "
        "or derived, and from what. If a rate is wrong for your organisation -- "
        "negotiated pricing, volume discounts -- edit that file and re-run; "
        "every figure moves with it."
    )
    out.append("")
    out.append("Source data:")
    out.append("")
    for source in frame.sources:
        label = source.profile or source.ingest_path
        status = f", schema `{source.profile_status}`" if source.profile_status else ""
        out.append(f"- `{source.path}` (via {label}{status})")
    out.append("")
    out.append(
        "This report was produced locally. No data was uploaded, and the tool "
        "has no network access on this path."
    )
    out.append("")
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
