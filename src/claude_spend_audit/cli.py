"""Command line entry point.

M1 ships one command, ``inspect``: read the data, print the §5 header block, and
say what is missing. There is no check logic here yet.

Deliberately stdlib-only. ``rich`` arrives with the terminal renderer in M2; a
prospect's security team should be able to read this project's whole dependency
list in five minutes (§7).
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from . import __version__
from .frame import TOKEN_FIELD_LABELS, TOKEN_FIELDS, Frame, merge
from .gaps import Severity
from .ingest import load_cost_csv, load_usage_csv
from .ingest.schema import Profile, SchemaError, SchemaMismatch
from .pricing import PricingError, PricingTable

_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-spend-audit",
        description=(
            "Find cost inefficiencies in Anthropic Usage & Cost data. "
            "Runs entirely on your machine; nothing is uploaded."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser(
        "inspect",
        help="summarise a usage export and report what the data cannot show",
    )
    inspect.add_argument("usage", type=Path, help="Console Usage page CSV export")
    inspect.add_argument(
        "--cost", type=Path, default=None, help="Console Cost page CSV export"
    )
    inspect.add_argument(
        "--pricing",
        type=Path,
        default=None,
        help="pricing JSON file (default: newest in pricing/)",
    )
    inspect.add_argument(
        "--usage-profile", type=Path, default=None, help="override the usage CSV schema"
    )
    inspect.add_argument(
        "--cost-profile", type=Path, default=None, help="override the cost CSV schema"
    )
    inspect.add_argument(
        "--explain-schema",
        action="store_true",
        help="print which CSV column mapped to which field, then continue",
    )
    inspect.add_argument("--json", action="store_true", help="emit JSON instead of text")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "inspect":
        return _inspect(args)
    return 2  # pragma: no cover - argparse rejects unknown commands first


def _inspect(args) -> int:
    try:
        pricing = (
            PricingTable.load(args.pricing)
            if args.pricing
            else PricingTable.load_default()
        )
    except PricingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        usage_profile = (
            Profile.load(args.usage_profile)
            if args.usage_profile
            else Profile.load_named("console_usage_csv")
        )
        usage_frame, usage_mapping = load_usage_csv(args.usage, usage_profile)

        cost_frame = None
        cost_mapping = None
        cost_profile = None
        if args.cost:
            cost_profile = (
                Profile.load(args.cost_profile)
                if args.cost_profile
                else Profile.load_named("console_cost_csv")
            )
            cost_frame, cost_mapping = load_cost_csv(args.cost, cost_profile)
    except SchemaMismatch as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (SchemaError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.explain_schema:
        print(usage_mapping.explain(usage_profile))
        if cost_mapping is not None and cost_profile is not None:
            print()
            print(cost_mapping.explain(cost_profile))
        print()

    frame = merge(usage_frame, cost_frame) if cost_frame else usage_frame
    if not frame.cost:
        from . import gaps as gapmod

        frame = frame.with_gaps([gapmod.no_cost_data()])

    estimate = pricing.estimate_frame(frame)
    frame = frame.with_gaps(estimate.gaps)

    if args.json:
        print(json.dumps(_as_json(frame, pricing, estimate), indent=2))
    else:
        print(_render(frame, pricing, estimate))

    # Exit 1 when a gap invalidates the headline total, so a script wrapping
    # this cannot mistake a caveated number for a clean one.
    return 1 if any(g.invalidates_total for g in frame.gaps) else 0


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _usd(amount: Decimal) -> str:
    """Format money with the sign outside the symbol: -$3.00, not $-3.00."""
    rounded = amount.quantize(Decimal("0.01"))
    sign = "-" if rounded < 0 else ""
    return f"{sign}${abs(rounded):,}"


def _render(frame: Frame, pricing: PricingTable, estimate) -> str:
    out: list[str] = []
    rule = "=" * 72

    out.append(rule)
    out.append("claude-spend-audit -- ingest summary")
    out.append(rule)

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
        out.append(f"  source           {source.path}  via {label}{status}")

    # -- spend ---------------------------------------------------------
    out.append("")
    out.append("SPEND")
    billed = frame.billed_total_usd
    if billed is not None:
        out.append(f"  billed (from cost data)      {_usd(billed):>16}")
    else:
        out.append(f"  billed (from cost data)      {'not supplied':>16}")
    out.append(f"  estimated (usage x pricing)  {_usd(estimate.usd):>16}")

    # The two are never merged. Their difference is the signal that surfaces
    # Priority Tier understatement (§2) rather than hiding it in a total.
    if billed is not None and estimate.usd:
        delta = estimate.usd - billed
        pct = (delta / billed * 100) if billed else Decimal("0")
        out.append(
            f"  difference                   {_usd(delta):>16}"
            f"  ({pct.quantize(Decimal('0.1'))}%)"
        )
        out.append(
            "    A large gap is diagnostic, not cosmetic: it can mean Priority"
        )
        out.append(
            "    Tier spend missing from cost data, negotiated rates the pricing"
        )
        out.append("    file does not know about, or a mis-mapped CSV column.")

    if not estimate.is_complete:
        out.append(
            f"  NOT PRICED: {estimate.unpriced_tokens:,} tokens have no published "
            "rate and are"
        )
        out.append(
            "    excluded from the estimate above rather than counted as zero."
        )

    # -- tokens --------------------------------------------------------
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

    # -- dimensions ----------------------------------------------------
    out.append("")
    out.append("DIMENSIONS PRESENT")
    if frame.grouped_by:
        for dim in sorted(frame.grouped_by):
            values = frame.distinct(dim)
            shown = ", ".join(str(v) if v is not None else "(null)" for v in values[:6])
            more = f", +{len(values) - 6} more" if len(values) > 6 else ""
            out.append(f"  {dim:<18} {len(values):>3} distinct  {shown}{more}")
    else:
        out.append("  (none -- this export carries no grouping columns)")

    # -- caveats -------------------------------------------------------
    out.append("")
    out.append("WHAT THIS DATA CANNOT SHOW")
    if not frame.gaps:
        out.append("  (nothing recorded)")
    for gap in sorted(frame.gaps, key=lambda g: _SEVERITY_ORDER[g.severity]):
        marker = {
            Severity.CRITICAL: "!!",
            Severity.WARNING: " !",
            Severity.INFO: " -",
        }[gap.severity]
        scope = f" [{gap.scope}]" if gap.scope else ""
        out.append("")
        out.append(f"  {marker}{scope} {_wrap(gap.message, 66, '        ')}")
        if gap.remedy:
            out.append(f"        -> {_wrap(gap.remedy, 62, '           ')}")

    out.append("")
    out.append(rule)
    out.append("No analysis run: this is M1 (ingest). Checks arrive in M2.")
    out.append("All processing happened locally. Nothing was uploaded.")
    out.append(rule)
    return "\n".join(out)


def _wrap(text: str, width: int, indent: str) -> str:
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


def _as_json(frame: Frame, pricing: PricingTable, estimate) -> dict:
    billed = frame.billed_total_usd
    return {
        "period": {
            "start": frame.period_start.isoformat() if frame.period_start else None,
            "end": frame.period_end.isoformat() if frame.period_end else None,
            "bucket_width": frame.bucket_width,
        },
        "pricing": {
            "version": pricing.version,
            "fetched_at": pricing.fetched_at,
            "source_url": pricing.source_url,
        },
        "sources": [
            {
                "path": s.path,
                "ingest_path": s.ingest_path,
                "profile": s.profile,
                "profile_status": s.profile_status,
                "unmapped_columns": list(s.unmapped_columns),
            }
            for s in frame.sources
        ],
        "spend": {
            # Strings, not floats: these are money and a JSON float would
            # silently round them.
            "billed_usd": str(billed) if billed is not None else None,
            "estimated_usd": str(estimate.usd),
            "estimate_is_complete": estimate.is_complete,
            "unpriced_tokens": estimate.unpriced_tokens,
        },
        "tokens": frame.tokens.as_dict() | {"total": frame.tokens.total},
        "web_search_requests": frame.web_search_requests,
        "grouped_by": sorted(frame.grouped_by),
        "gaps": [
            {
                "code": str(g.code),
                "severity": str(g.severity),
                "scope": g.scope,
                "message": g.message,
                "invalidates_total": g.invalidates_total,
                "remedy": g.remedy,
            }
            for g in sorted(frame.gaps, key=lambda g: _SEVERITY_ORDER[g.severity])
        ],
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
