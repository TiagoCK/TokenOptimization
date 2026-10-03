"""Terminal output. Stdlib only.

PROJECT_BRIEF.md §7 names ``rich``, but its stated reason for a short
dependency list is that "a security-conscious prospect can audit it in five
minutes -- this is a feature, not an aesthetic preference". Zero dependencies
is a stronger version of that property than four, and for a tool whose entire
pitch is "run this on your own data" an empty dependency list is worth more
than box-drawing characters.

§5 requires: a ranked table, largest estimated impact first, with the
confidence level visible on every row. The renderer iterates whatever findings
it is handed and knows nothing about which checks exist -- §7's requirement
that adding a check never means touching the reporter.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from ..findings import Confidence, Evidence, Finding
from ..frame import TOKEN_FIELD_LABELS, TOKEN_FIELDS, Frame
from ..gaps import Gap, Severity
from ..pricing import PricingTable

WIDTH = 78
RULE = "=" * WIDTH

_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}

_SEVERITY_MARKER = {
    Severity.CRITICAL: "!!",
    Severity.WARNING: " !",
    Severity.INFO: " -",
}

# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------


def usd(amount: Decimal) -> str:
    """Money with the sign outside the symbol: -$3.00, not $-3.00."""
    rounded = amount.quantize(Decimal("0.01"))
    sign = "-" if rounded < 0 else ""
    return f"{sign}${abs(rounded):,}"


def wrap(text: str, width: int, indent: str) -> str:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return f"\n{indent}".join(lines)


def elide(text: str, width: int) -> str:
    """Shorten a single unbreakable token to `width`, losing its middle.

    ``wrap`` splits on whitespace, so it cannot help with a filesystem path:
    one long word either fits or overruns. A path that overruns breaks the
    78-column promise in the one place it is most likely to -- a deep
    checkout directory -- so the middle goes and both ends stay, keeping the
    filename, which is the part a reader needs.
    """
    if len(text) <= width:
        return text
    if width <= 3:  # pragma: no cover - callers pass real column widths
        return text[:width]
    keep = width - 3
    head = (keep + 1) // 2
    return f"{text[:head]}...{text[len(text) - (keep - head):]}"


def _table(evidence: Evidence, indent: str = "    ") -> list[str]:
    """A plain aligned table, kept inside WIDTH.

    Terminal output gets pasted into emails and tickets, where a 105-column
    row wraps into unreadable rubble. When the natural widths do not fit, the
    widest column is trimmed (with an ellipsis) until they do -- identifiers
    lose their middle rather than every row losing its shape.
    """
    widths = [len(c) for c in evidence.columns]
    for row in evidence.rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    # Shrink the widest column first, repeatedly. Narrow columns (counts,
    # money) keep their full value; a long identifier gives up its middle.
    available = WIDTH - len(indent) - 2 * (len(widths) - 1)
    while sum(widths) > available and max(widths) > 6:
        widths[widths.index(max(widths))] -= 1

    def clip(cell: str, width: int) -> str:
        return elide(cell, width).ljust(width)

    def line(cells: Sequence[str]) -> str:
        return indent + "  ".join(
            clip(c, widths[i]) for i, c in enumerate(cells)
        ).rstrip()

    out = [line(evidence.columns), indent + "  ".join("-" * w for w in widths)]
    out.extend(line(row) for row in evidence.rows)
    if evidence.note:
        out.append("")
        out.append(f"{indent}{wrap(evidence.note, WIDTH - len(indent) - 2, indent)}")
    return out


# --------------------------------------------------------------------------
# header block -- §5's "header block should always state ..."
# --------------------------------------------------------------------------


def render_header(frame: Frame, pricing: PricingTable, estimate, *, title: str) -> list[str]:
    out = [RULE, f"claude-spend-audit -- {title}", RULE]

    start, end = frame.period_start, frame.period_end
    period = (
        f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC"
        if start and end
        else "unknown"
    )
    out.append(f"  period covered   {period}")
    out.append(f"  granularity      {frame.bucket_width or 'indeterminate'}")
    out.append(f"  usage rows       {len(frame.usage):,}")
    out.append(f"  cost rows        {len(frame.cost):,}")
    out.append(f"  pricing file     {pricing.version} (fetched {pricing.fetched_at})")
    for source in frame.sources:
        label = source.profile or source.ingest_path
        status = f" [{source.profile_status}]" if source.profile_status else ""
        prefix = "  source           "
        out.append(prefix + elide(str(source.path), WIDTH - len(prefix)))
        out.append(f"                   via {label}{status}")

    out.append("")
    out.append("SPEND")
    billed = frame.billed_total_usd
    if billed is not None:
        out.append(f"  billed (from cost data)      {usd(billed):>16}")
    else:
        out.append(f"  billed (from cost data)      {'not supplied':>16}")
    out.append(f"  estimated (usage x pricing)  {usd(estimate.usd):>16}")

    # Never merged. The difference is what surfaces Priority Tier
    # understatement (§2) instead of burying it inside one total.
    if billed is not None and estimate.usd:
        delta = estimate.usd - billed
        pct = (delta / billed * 100) if billed else Decimal("0")
        out.append(
            f"  difference                   {usd(delta):>16}"
            f"  ({pct.quantize(Decimal('0.1'))}%)"
        )
        out.append("    A large gap is diagnostic, not cosmetic: it can mean Priority")
        out.append("    Tier spend missing from cost data, negotiated rates the pricing")
        out.append("    file does not know about, or a mis-mapped CSV column.")

    if not estimate.is_complete:
        out.append(
            f"  NOT PRICED: {estimate.unpriced_tokens:,} tokens have no published "
            "rate and are"
        )
        out.append("    excluded from the estimate above rather than counted as zero.")

    tokens = frame.tokens
    out.append("")
    out.append("TOKENS")
    for f in TOKEN_FIELDS:
        count = getattr(tokens, f)
        share = (count / tokens.total * 100) if tokens.total else 0
        out.append(f"  {TOKEN_FIELD_LABELS[f]:<18} {count:>16,}  {share:5.1f}%")
    out.append(f"  {'total':<18} {tokens.total:>16,}")
    if frame.web_search_requests:
        out.append(f"  {'web searches':<18} {frame.web_search_requests:>16,}")

    return out


def render_enterprise_header(report, pricing: PricingTable, *, title: str) -> list[str]:
    """The header block for a Team/Enterprise spend report.

    Its own function rather than a branch in ``render_header``: there is no
    time granularity, no billed-versus-estimated pair and no five token
    buckets, and pretending otherwise would print zeros the data never gave.
    Every count the export does not carry says so instead.
    """
    from ..enterprise import REPORT_BANNER
    from ..enterprise.report import FIELD_LABELS, INT_FIELDS

    out = [RULE, f"claude-spend-audit -- {title}", RULE]
    out.append("  report type      Team/Enterprise spend report")
    start, end = report.period_start, report.period_end
    if start and end:
        period = f"{start:%Y-%m-%d} to {end:%Y-%m-%d} UTC"
    else:
        period = "the range chosen at export (not stated in the file)"
    out.append(f"  period covered   {period}")
    out.append(f"  rows             {len(report.rows):,}")
    for name, label in (("user_email", "users"), ("product", "products"), ("model", "models")):
        if report.has(name):
            out.append(f"  {label:<16} {len(report.distinct(name)):,}")
        else:
            out.append(f"  {label:<16} not in export")
    out.append(
        f"  pricing file     {pricing.version} (fetched {pricing.fetched_at}), "
        "for tiers only"
    )
    for source in report.sources:
        label = source.profile or source.ingest_path
        status = f" [{source.profile_status}]" if source.profile_status else ""
        prefix = "  source           "
        out.append(prefix + elide(str(source.path), WIDTH - len(prefix)))
        out.append(f"                   via {label}{status}")

    out.append("")
    out.append(f"  {wrap(REPORT_BANNER, WIDTH - 4, '  ')}")

    out.append("")
    out.append("SPEND (as the report states it; not re-priced)")
    net = report.total("net_spend_usd")
    gross = report.total("gross_spend_usd")
    out.append(f"  net spend                    {usd(net) if net is not None else 'not in export':>16}")
    out.append(f"  gross spend                  {usd(gross) if gross is not None else 'not in export':>16}")
    if net is not None and gross:
        delta = net - gross
        pct = (-delta / gross * 100).quantize(Decimal("0.1"))
        out.append(f"  difference                   {usd(delta):>16}  ({pct}% below gross)")

    out.append("")
    out.append("TOKENS AND REQUESTS")
    for name in INT_FIELDS:
        value = report.total(name)
        shown = f"{value:,}" if value is not None else "not in export"
        out.append(f"  {FIELD_LABELS[name]:<18} {shown:>16}")
    return out


def render_dimensions(frame: Frame) -> list[str]:
    out = ["", "DIMENSIONS PRESENT"]
    if not frame.grouped_by:
        out.append("  (none -- this export carries no grouping columns)")
        return out

    # Workspace and key IDs are long. Fit as many as the line allows and say
    # how many were left off, rather than running past the width.
    for dim in sorted(frame.grouped_by):
        values = frame.distinct(dim)
        prefix = f"  {dim:<18} {len(values):>3} distinct  "
        # Reserve room for the ", +N more" suffix so adding it cannot overflow.
        budget = WIDTH - len(prefix) - len(f", +{len(values)} more")

        labels = [str(v) if v is not None else "(null)" for v in values]
        shown: list[str] = []
        used = 0
        for label in labels:
            cost = len(label) + (2 if shown else 0)
            # Always keep at least one, even if it alone overruns the budget.
            if shown and used + cost > budget:
                break
            shown.append(label)
            used += cost
        more = f", +{len(labels) - len(shown)} more" if len(shown) < len(labels) else ""
        out.append(f"{prefix}{', '.join(shown)}{more}")
    return out


def render_gaps(gaps: Sequence[Gap]) -> list[str]:
    out = ["", "WHAT THIS DATA CANNOT SHOW"]
    if not gaps:
        out.append("  (nothing recorded)")
    for gap in sorted(gaps, key=lambda g: _SEVERITY_ORDER[g.severity]):
        marker = _SEVERITY_MARKER[gap.severity]
        # The scope is folded into the wrapped text rather than sitting in the
        # prefix: a long scope like [console_cost] would otherwise push the
        # first line past the width while every later line fits.
        body = f"[{gap.scope}] {gap.message}" if gap.scope else gap.message
        out.append("")
        out.append(f"  {marker} {wrap(body, WIDTH - 8, '        ')}")
        if gap.remedy:
            out.append(f"        -> {wrap(gap.remedy, WIDTH - 12, '           ')}")
    return out


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------


def render_findings(findings: Sequence[Finding]) -> list[str]:
    """The ranked table, then a detail block per finding.

    Findings arrive already ranked (measured before candidate before
    observation, dollars descending within each). The renderer does not
    re-sort: ordering is a judgement about inference strength and belongs with
    the findings, not the presentation.
    """
    out: list[str] = ["", "FINDINGS"]
    if not findings:
        out.append("  No findings. Every check that could run found nothing to report.")
        return out

    out.append("")
    out.append(
        f"  {'#':<3} {'check':<6} {'confidence':<11} "
        f"{'measured':>12}  {'recoverable':>20}"
    )
    out.append(
        f"  {'-' * 3} {'-' * 6} {'-' * 11} {'-' * 12}  {'-' * 20}"
    )
    for i, finding in enumerate(findings, start=1):
        measured = usd(finding.measured_usd) if finding.measured_usd is not None else "-"
        # "not claimed" is not a hedge -- it is the finding saying the data
        # cannot support a savings number, which §3 requires it to say.
        recoverable = str(finding.recoverable) if finding.recoverable else "not claimed"
        out.append(
            f"  {i:<3} {finding.check_id:<6} "
            f"{finding.confidence:<11} "
            f"{measured:>12}  {recoverable:>20}"
        )
        out.append(f"      {wrap(finding.title, 68, '      ')}")

    for i, finding in enumerate(findings, start=1):
        out.extend(_render_detail(i, finding))

    return out


def _render_detail(index: int, finding: Finding) -> list[str]:
    heading = f"{index}. [{finding.check_id}] {finding.title}"
    out = ["", "-" * WIDTH, wrap(heading, WIDTH, "   ")]
    out.append(f"   confidence: {finding.confidence}" + (f"   scope: {finding.scope}" if finding.scope else ""))
    out.append("")
    out.append(f"   {wrap(finding.summary, 72, '   ')}")

    if finding.measured_usd is not None:
        out.append("")
        out.append(f"   measured    {usd(finding.measured_usd)}")
        out.append(f"               {wrap(finding.measured_basis or '', 62, '               ')}")

    if finding.recoverable is not None:
        out.append("")
        out.append(f"   recoverable {finding.recoverable}")
        # §3: the assumptions go here, inline, next to the figure -- never in
        # a footnote the reader can skip.
        for assumption in finding.recoverable.assumptions:
            out.append(f"               - {wrap(assumption, 60, '                 ')}")
    else:
        out.append("")
        out.append("   recoverable not claimed -- this figure measures what was spent,")
        out.append("               not what could be saved.")

    if finding.evidence is not None:
        out.append("")
        out.append("   evidence")
        out.extend(_table(finding.evidence, indent="     "))

    if finding.remedy:
        out.append("")
        out.append(f"   what to do  {wrap(finding.remedy, 62, '               ')}")

    if finding.caveats:
        out.append("")
        out.append("   caveats")
        for caveat in finding.caveats:
            out.append(f"     - {wrap(caveat, 68, '       ')}")

    return out


def render_skipped(skipped: Sequence[tuple[str, str]]) -> list[str]:
    """Checks that could not run. Silence about them would be misleading."""
    if not skipped:
        return []
    out = ["", "CHECKS THAT COULD NOT RUN"]
    for label, _ in skipped:
        out.append(f"  - {label}")
    out.append("")
    out.append("  Reasons are in the caveats above, with what to re-export.")
    return out


def footer(lines: Sequence[str] = ()) -> list[str]:
    out = ["", RULE]
    out.extend(lines)
    out.append("All processing happened locally. Nothing was uploaded.")
    out.append(RULE)
    return out
