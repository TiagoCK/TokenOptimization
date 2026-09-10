"""Command line entry point.

Two commands:

* ``inspect`` -- what the data covers and what it cannot show. No checks.
* ``audit``   -- runs the registered checks and prints the ranked findings.

Rendering lives in ``render.terminal``; this module handles arguments, loading,
and exit codes only.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import __version__
from . import gaps as gapmod
from .checks import CHECKS, CheckConfig, run_checks
from .frame import Frame, merge
from .ingest import load_cost_csv, load_usage_csv
from .ingest.schema import Profile, SchemaError, SchemaMismatch
from .pricing import PricingError, PricingTable
from .render import markdown, terminal

_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


# --------------------------------------------------------------------------
# arguments
# --------------------------------------------------------------------------


def _add_data_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("usage", type=Path, help="Console Usage page CSV export")
    parser.add_argument(
        "--cost", type=Path, default=None, help="Console Cost page CSV export"
    )
    parser.add_argument(
        "--pricing",
        type=Path,
        default=None,
        help="pricing JSON file (default: newest in pricing/)",
    )
    parser.add_argument(
        "--usage-profile", type=Path, default=None, help="override the usage CSV schema"
    )
    parser.add_argument(
        "--cost-profile", type=Path, default=None, help="override the cost CSV schema"
    )
    parser.add_argument(
        "--explain-schema",
        action="store_true",
        help="print which CSV column mapped to which field, then continue",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")


def _business_hours(text: str) -> tuple[int, int]:
    try:
        start, _, end = text.partition("-")
        first, last = int(start.split(":")[0]), int(end.split(":")[0])
    except (ValueError, IndexError) as exc:
        raise argparse.ArgumentTypeError(
            f"expected a window like 09:00-18:00, got {text!r}"
        ) from exc
    if not (0 <= first < 24 and 0 < last <= 24 and first < last):
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a valid window inside a single day"
        )
    return first, last


def _decimal(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from exc


def _share(text: str) -> Decimal:
    """A fraction in [0, 1]. Accepts "0.25" or "25%"."""
    cleaned = text.strip().rstrip("%")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from exc
    if text.strip().endswith("%"):
        value /= 100
    if not (0 <= value <= 1):
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a share between 0 and 1 (or 0% and 100%)"
        )
    return value


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
    _add_data_args(inspect)

    audit = sub.add_parser(
        "audit", help="run the checks and print ranked findings"
    )
    _add_data_args(audit)
    audit.add_argument(
        "--min-impact",
        type=_decimal,
        default=Decimal("1"),
        metavar="USD",
        help="drop findings below this dollar amount (default: 1)",
    )
    audit.add_argument(
        "--spike-threshold",
        type=_decimal,
        default=Decimal("3"),
        metavar="X",
        help="flag a day at this multiple of the trailing median (default: 3)",
    )
    audit.add_argument(
        "--utc-offset",
        type=_decimal,
        default=Decimal("0"),
        metavar="HOURS",
        help=(
            "hours from UTC defining your local business day, e.g. -5 or +5.5 "
            "(default: 0). Daylight saving is not modelled."
        ),
    )
    audit.add_argument(
        "--business-hours",
        type=_business_hours,
        default=(9, 18),
        metavar="HH:MM-HH:MM",
        help="local business-hours window (default: 09:00-18:00)",
    )
    audit.add_argument(
        "--format",
        choices=("terminal", "markdown"),
        default="terminal",
        help="output format (default: terminal). markdown is the shareable one",
    )
    audit.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="write the report to a file instead of stdout",
    )
    audit.add_argument(
        "--cache-prefix-share",
        type=_share,
        default=None,
        metavar="0-1",
        help=(
            "what share of uncached input is a cacheable prefix. Raises C2's "
            "savings floor above $0 -- your estimate, not a measurement"
        ),
    )
    audit.add_argument(
        "--batch-eligible-share",
        type=_share,
        default=None,
        metavar="0-1",
        help=(
            "what share of standard-tier spend tolerates async processing. "
            "Raises C3's savings floor above $0 -- your estimate"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "inspect":
        return _inspect(args)
    if args.command == "audit":
        return _audit(args)
    return 2  # pragma: no cover - argparse rejects unknown commands first


# --------------------------------------------------------------------------
# loading, shared by both commands
# --------------------------------------------------------------------------


class _LoadFailed(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def _load(args) -> tuple[Frame, PricingTable, object]:
    try:
        pricing = (
            PricingTable.load(args.pricing)
            if args.pricing
            else PricingTable.load_default()
        )
    except PricingError as exc:
        raise _LoadFailed(str(exc), 2) from exc

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
        raise _LoadFailed(str(exc), 1) from exc
    except (SchemaError, FileNotFoundError) as exc:
        raise _LoadFailed(str(exc), 2) from exc

    if args.explain_schema:
        print(usage_mapping.explain(usage_profile))
        if cost_mapping is not None and cost_profile is not None:
            print()
            print(cost_mapping.explain(cost_profile))
        print()

    frame = merge(usage_frame, cost_frame) if cost_frame else usage_frame
    if not frame.cost:
        frame = frame.with_gaps([gapmod.no_cost_data()])

    estimate = pricing.estimate_frame(frame)
    frame = frame.with_gaps(estimate.gaps)
    return frame, pricing, estimate


def _exit_code(frame: Frame) -> int:
    """1 when a gap means the headline total understates the real bill.

    Findings do not affect this: "we found something" is the tool working, not
    an error.
    """
    return 1 if any(g.invalidates_total for g in frame.gaps) else 0


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------


def _inspect(args) -> int:
    try:
        frame, pricing, estimate = _load(args)
    except _LoadFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code

    # Evaluate check preconditions without running anything, so `inspect` can
    # say which checks this export supports. The registry is the single source
    # of truth for that -- ingest no longer hardcodes check names.
    blocked: list[tuple[str, str]] = []
    for check in CHECKS:
        unmet = check.unmet(frame)
        if unmet:
            frame = frame.with_gaps(unmet)
            blocked.append((check.label, "; ".join(g.message for g in unmet)))

    if args.json:
        payload = _as_json(frame, pricing, estimate)
        payload["checks_blocked"] = [label for label, _ in blocked]
        print(json.dumps(payload, indent=2))
        return _exit_code(frame)

    out = terminal.render_header(frame, pricing, estimate, title="ingest summary")
    out.extend(terminal.render_dimensions(frame))
    out.extend(terminal.render_gaps(frame.gaps))
    out.extend(terminal.render_skipped(blocked))
    out.extend(
        terminal.footer(
            [
                f"No checks run. {len(CHECKS) - len(blocked)} of {len(CHECKS)} would "
                "run on this data -- use `audit`.",
            ]
        )
    )
    print("\n".join(out))
    return _exit_code(frame)


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------


def _audit(args) -> int:
    try:
        frame, pricing, estimate = _load(args)
    except _LoadFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code

    start, end = args.business_hours
    config = CheckConfig(
        min_impact_usd=args.min_impact,
        spike_threshold=args.spike_threshold,
        utc_offset_hours=args.utc_offset,
        business_hour_start=start,
        business_hour_end=end,
        cache_prefix_share=args.cache_prefix_share,
        batch_eligible_share=args.batch_eligible_share,
    )

    run = run_checks(frame, pricing, config)
    frame = frame.with_gaps(run.gaps)

    if args.json:
        payload = _as_json(frame, pricing, estimate)
        payload["findings"] = [_finding_json(f) for f in run.findings]
        payload["checks_blocked"] = [label for label, _ in run.skipped]
        payload["checks_clean"] = list(run.clean)
        print(json.dumps(payload, indent=2))
        return _exit_code(frame)

    if args.format == "markdown":
        # The shareable artifact. It carries its own provenance because it gets
        # forwarded away from the terminal that produced it (§5).
        report = markdown.render(
            frame,
            pricing,
            estimate,
            run.findings,
            skipped=run.skipped,
            version=__version__,
        )
    else:
        out = terminal.render_header(frame, pricing, estimate, title="audit")
        out.extend(terminal.render_findings(run.findings))
        out.extend(terminal.render_skipped(run.skipped))
        out.extend(terminal.render_gaps(frame.gaps))
        out.extend(
            terminal.footer(
                [
                    f"{len(run.findings)} finding(s) from "
                    f"{len(CHECKS) - len(run.skipped)} of {len(CHECKS)} checks.",
                ]
            )
        )
        report = "\n".join(out)

    if args.output:
        args.output.write_text(report + "\n", encoding="utf-8")
        # To stderr, so `-o` plus a pipe stays clean.
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(report)
    return _exit_code(frame)


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------


def _finding_json(finding) -> dict:
    return {
        "check_id": finding.check_id,
        "title": finding.title,
        "confidence": str(finding.confidence),
        "scope": finding.scope,
        "summary": finding.summary,
        # Money as strings: a JSON float would round it.
        "measured_usd": (
            str(finding.measured_usd) if finding.measured_usd is not None else None
        ),
        "measured_basis": finding.measured_basis,
        "recoverable": (
            {
                "low": str(finding.recoverable.low),
                "high": str(finding.recoverable.high),
                "assumptions": list(finding.recoverable.assumptions),
            }
            if finding.recoverable
            else None
        ),
        "evidence": (
            {
                "columns": list(finding.evidence.columns),
                "rows": [list(r) for r in finding.evidence.rows],
                "note": finding.evidence.note,
            }
            if finding.evidence
            else None
        ),
        "remedy": finding.remedy,
        "caveats": list(finding.caveats),
    }


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
            for g in sorted(frame.gaps, key=lambda g: _SEVERITY_ORDER[str(g.severity)])
        ],
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
