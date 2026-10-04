"""Command line entry point.

Three commands:

* ``inspect`` -- what the data covers and what it cannot show. No checks.
* ``audit``   -- runs the registered checks and prints the ranked findings.
* ``fetch``   -- downloads usage or cost from the Admin API to a file, and
  stops. Analysis then runs on that file, offline.

``inspect`` and ``audit`` also accept a Team/Enterprise plan spend report.
It is recognised by its columns and routed to its own pipeline -- its own data
model and its own descriptive checks -- because it is not usage data.

``fetch --source analytics`` pulls the same kind of data a Claude Enterprise
organisation has no Admin API for, from the Enterprise Analytics API, into one
file that ``audit`` reads like any other saved fetch.

Rendering lives in ``render.terminal``; this module handles arguments, loading,
and exit codes only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import __version__
from . import gaps as gapmod
from .checks import CHECKS, CheckConfig, CheckRun, run_checks
from .describe import describe as describe_file
from .findings import rank
from .enterprise import ENTERPRISE_CHECKS, EnterpriseConfig, run_enterprise_checks
from .enterprise import groups as rbac
from .enterprise.report import INT_FIELDS, SpendReport
from .frame import Frame, merge
from .ingest import load_cost_csv, load_usage_csv
from .ingest.api_client import (
    ANALYTICS_COST_PATH,
    ANALYTICS_USAGE_GROUP_BY,
    ANALYTICS_USAGE_PATH,
    ANALYTICS_USER_COST_PATH,
    ANALYTICS_USER_USAGE_PATH,
    ApiClient,
    ApiError,
    AuthError,
    Credential,
    analytics_envelope,
    claude_code_envelope,
    envelope,
    is_analytics_envelope,
    is_claude_code_envelope,
    is_envelope,
)
from .claude_code import CODE_CHECKS, CodeConfig, run_code_checks
from .ingest.claude_code_json import ClaudeCodeFileError, load_claude_code
from .ingest.analytics_json import AnalyticsFileError, load_analytics
from .ingest.api_json import ResponseShapeError, load_cost_json, load_usage_json
from .ingest.enterprise_csv import ENTERPRISE_KIND, detect_enterprise, load_enterprise_csv
from .ingest.schema import Profile, SchemaError, SchemaMismatch
from .gaps import GapCode
from .pricing import PRICE_DIMENSIONS, PricingError, PricingTable
from .render import markdown, terminal

_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}

#: What `fetch` breaks usage out by unless told otherwise. Two jobs:
#: every dimension a registered check declares in `needs_dimensions`, and
#: every dimension that changes a token's price (pricing.PRICE_DIMENSIONS) --
#: without inference_geo and speed, US-only and fast-mode traffic would be
#: priced at the baseline with nothing in the numbers to show it. A test
#: holds this against the registry so the two cannot drift apart.
#: `speed` needs a beta header, which ApiClient adds by itself.
DEFAULT_FETCH_GROUP_BY: tuple[str, ...] = (
    "model",
    "workspace_id",
    "api_key_id",
    "service_tier",
    "context_window",
    "inference_geo",
    "speed",
)

#: What `fetch --source analytics` groups the usage report by. Every
#: dimension a usage check needs that this API has, plus product. Never
#: rbac_group_id: its rows overlap, so a Frame built from them would count a
#: user in two groups twice.
DEFAULT_ANALYTICS_GROUP_BY: tuple[str, ...] = (
    "model",
    "product",
    "context_window",
    "inference_geo",
    "speed",
)

#: Where each source's key is read from unless --api-key-env says otherwise.
_DEFAULT_KEY_ENV = {
    "admin": "ANTHROPIC_ADMIN_KEY",
    "analytics": "ANTHROPIC_ANALYTICS_KEY",
    "claude-code": "ANTHROPIC_ADMIN_KEY",
}


# --------------------------------------------------------------------------
# arguments
# --------------------------------------------------------------------------


def _add_data_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "usage",
        type=Path,
        help=(
            "Console Usage page CSV export, a file saved by `fetch`, or a "
            "Team/Enterprise plan spend report"
        ),
    )
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
    parser.add_argument(
        "--anonymize",
        action="store_true",
        help=(
            "replace user emails with user-001, user-002... (largest spender "
            "first) in Team/Enterprise per-user output, and show workspace and "
            "API-key IDs instead of their names, which often name people"
        ),
    )
    parser.add_argument(
        "--date-order",
        choices=("mdy", "dmy"),
        default=None,
        help=(
            "how to read slash dates such as 03/08/2026 when the file cannot "
            "settle it: mdy (US, March 8th) or dmy (3rd August)"
        ),
    )


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


def _utc_date(text: str) -> "datetime":
    """A UTC date or timestamp. Never naive -- see api_client._rfc3339."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    raw = text.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return _dt.strptime(raw, fmt).replace(tzinfo=_tz.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"expected YYYY-MM-DD (UTC), got {text!r}"
    )


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
    describe = sub.add_parser(
        "describe",
        help="print a file's format -- columns, mappings, value shapes -- with none of its data",
        description=(
            "Describe a usage, cost or spend-report CSV, or a file saved by "
            "`fetch`, without printing any of its contents: which profile "
            "matched, which column maps to which field, the shape of each "
            "column's values, and what audit would do with it. Safe to paste "
            "into an issue when a file is refused or mis-read."
        ),
    )
    describe.add_argument("file", type=Path, help="the file to describe")
    describe.add_argument(
        "--date-order",
        choices=("mdy", "dmy"),
        default=None,
        help="as for audit: how to read ambiguous slash dates in the dry run",
    )

    fetch = sub.add_parser(
        "fetch",
        # allow_abbrev=False is load-bearing here, not tidiness. With argparse's
        # default prefix matching, `--api-key sk-ant-...` silently resolves to
        # `--api-key-env` and the secret becomes the *variable name* -- which
        # then gets echoed into the "not found in $..." error on stderr. The
        # one flag family on this command is the one that must never do that.
        allow_abbrev=False,
        help=(
            "download Usage & Cost data (Admin API) or Claude Enterprise "
            "analytics to a local file"
        ),
        description=(
            "Fetches once and saves the raw API responses, so analysis can be "
            "re-run offline at any thresholds without hitting the API again. "
            "The saved file is yours: nothing is uploaded anywhere."
        ),
    )
    fetch.add_argument(
        "--source",
        choices=("admin", "analytics", "claude-code"),
        default="admin",
        help=(
            "admin: the Usage & Cost Admin API (Developer Console "
            "organisations). analytics: the Claude Enterprise Analytics API, "
            "with an Analytics API key. claude-code: the Claude Code Analytics "
            "API, with the Admin API key (default: admin)"
        ),
    )
    fetch.add_argument(
        "--no-users",
        action="store_true",
        help=(
            "analytics only: skip the per-user reports, so the saved file "
            "holds no email addresses"
        ),
    )
    fetch.add_argument(
        "--no-names",
        action="store_true",
        help=(
            "admin only: skip looking up workspace and API-key names, so "
            "findings show IDs and the saved file holds no names"
        ),
    )
    fetch.add_argument(
        "--from",
        dest="start",
        type=_utc_date,
        required=True,
        metavar="YYYY-MM-DD",
        help="start of the range, inclusive (UTC)",
    )
    fetch.add_argument(
        "--to",
        dest="end",
        type=_utc_date,
        required=True,
        metavar="YYYY-MM-DD",
        help="end of the range, exclusive (UTC)",
    )
    fetch.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        metavar="PATH",
        help="where to write the fetched data",
    )
    fetch.add_argument(
        "--what",
        choices=("usage", "cost"),
        default="usage",
        help="which report to fetch (default: usage)",
    )
    fetch.add_argument(
        "--bucket-width",
        choices=("1m", "1h", "1d"),
        default="1d",
        help="time granularity for usage (default: 1d). C6b needs 1h",
    )
    fetch.add_argument(
        "--group-by",
        default=",".join(DEFAULT_FETCH_GROUP_BY),
        metavar="A,B,C",
        help=(
            "dimensions to break out, comma separated. The default covers "
            "every check that needs grouping and every dimension that changes "
            "a price; pass an empty string for none"
        ),
    )
    fetch.add_argument(
        "--api-key-env",
        default=None,
        metavar="NAME",
        help=(
            "environment variable holding the key (default: "
            "ANTHROPIC_ADMIN_KEY, or ANTHROPIC_ANALYTICS_KEY with --source "
            "analytics). The key itself is never accepted on the command line"
        ),
    )
    fetch.add_argument(
        "--window",
        action="store_true",
        help=(
            "split the range into windows so a failure loses one window rather "
            "than the whole pull. Pagination alone already handles long ranges"
        ),
    )
    fetch.add_argument(
        "--max-retries",
        type=int,
        default=4,
        metavar="N",
        help="retries for 429 and 5xx responses (default: 4)",
    )
    # Declared only so it can be refused with an explanation. Without it,
    # `--api-key ...` is an "unrecognized argument", which tells the user what
    # is wrong but not why the option does not exist.
    fetch.add_argument(
        "--api-key", dest="rejected_api_key", default=None, help=argparse.SUPPRESS
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
    if args.command == "fetch":
        return _fetch(args)
    if args.command == "describe":
        print(describe_file(args.file, date_order=args.date_order))
        return 0
    return 2  # pragma: no cover - argparse rejects unknown commands first


# --------------------------------------------------------------------------
# loading, shared by both commands
# --------------------------------------------------------------------------


def _profile_of(frame: Frame, explicit: Profile | None) -> Profile:
    """Recover the profile ingest actually used, for --explain-schema."""
    if explicit is not None:
        return explicit
    name = frame.sources[0].profile
    return Profile.load_named(f"{name}_csv")


def _read_saved(path) -> dict | None:
    """Return a `fetch` envelope if this file is one, else None.

    Detected by content, not by extension: a file named .json that is not one
    of ours should fall through to the CSV path and get that path's errors.
    """
    if path is None:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    known = (is_envelope, is_analytics_envelope, is_claude_code_envelope)
    return payload if any(test(payload) for test in known) else None


def _frame_from_saved(saved: dict, expected: str) -> Frame:
    """Rebuild a Frame from a saved fetch.

    `group_by` comes from the envelope rather than being inferred, which is
    what keeps Frame.grouped_by honest: a response cannot report which
    dimensions were requested, since an ungrouped dimension and a genuinely
    null one both come back as null.
    """
    endpoint = saved.get("endpoint")
    if endpoint != expected:
        raise _LoadFailed(
            f"this file holds {endpoint!r} data but was given as the "
            f"{expected!r} argument",
            2,
        )
    request = saved.get("request") or {}
    pages = saved.get("pages") or []
    source = f"fetch {request.get('starting_at')}..{request.get('ending_at')}"
    if expected == "cost":
        frame = load_cost_json(pages, source=source)
    else:
        frame = load_usage_json(pages, group_by=request.get("group_by") or (), source=source)
    names = saved.get("names")
    if isinstance(names, dict):
        frame = replace(
            frame,
            names={d: dict(m) for d, m in names.items() if isinstance(m, dict)},
        )
    return frame


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

    # A file saved by `fetch` is JSON, not CSV. Routing on content rather than
    # on a flag means `audit raw.json` just works.
    saved = _read_saved(args.usage)
    if saved is not None:
        try:
            usage_frame = _frame_from_saved(saved, "usage")
            cost_frame = _read_saved(args.cost) if args.cost else None
            if cost_frame is not None:
                cost_frame = _frame_from_saved(cost_frame, "cost")
        except (ResponseShapeError, InvalidOperation) as exc:
            raise _LoadFailed(f"the saved response cannot be read: {exc}", 2) from exc
        frame = merge(usage_frame, cost_frame) if cost_frame else usage_frame
        if args.anonymize:
            # Key names are often a person's ("alice-laptop"); IDs are not.
            frame = replace(frame, names={})
        if not frame.cost:
            frame = frame.with_gaps([gapmod.no_cost_data()])
        frame = frame.with_gaps(_period_gaps(usage_frame, cost_frame))
        frame, merged = pricing.merge_model_aliases(frame)
        frame = frame.with_gaps(merged)
        estimate = pricing.estimate_frame(frame)
        frame = frame.with_gaps(estimate.gaps)
        return frame.with_gaps(_scale_gaps(frame, estimate)), pricing, estimate

    try:
        # None means "auto-select": more than one producer writes these CSVs
        # and they disagree on column names and on cents-vs-dollars.
        usage_profile = (
            Profile.load(args.usage_profile) if args.usage_profile else None
        )
        usage_frame, usage_mapping = load_usage_csv(
            args.usage, usage_profile, date_order=args.date_order
        )
        usage_profile = _profile_of(usage_frame, usage_profile)

        cost_frame = None
        cost_mapping = None
        cost_profile = None
        if args.cost:
            cost_profile = (
                Profile.load(args.cost_profile) if args.cost_profile else None
            )
            cost_frame, cost_mapping = load_cost_csv(
                args.cost, cost_profile, date_order=args.date_order
            )
            cost_profile = _profile_of(cost_frame, cost_profile)
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
    frame = frame.with_gaps(_period_gaps(usage_frame, cost_frame))

    # Merge before estimating, so every figure downstream sees one segment per
    # model rather than one per spelling. Each merge is reported in the
    # caveats, naming the strings that were combined.
    frame, merged = pricing.merge_model_aliases(frame)
    frame = frame.with_gaps(merged)

    estimate = pricing.estimate_frame(frame)
    frame = frame.with_gaps(estimate.gaps)
    return frame.with_gaps(_scale_gaps(frame, estimate)), pricing, estimate


#: Billed and estimated cost this far apart, either way, cannot be a price
#: difference; at 100x it is the signature of cents read as dollars.
_SCALE_LIMIT = Decimal("20")


def _scale_gaps(frame: Frame, estimate) -> list:
    """A critical gap when billed and estimated cost differ by 20x or more."""
    billed = frame.billed_total_usd
    if not billed or not estimate.usd or billed <= 0 or estimate.usd <= 0:
        return []
    ratio = billed / estimate.usd
    if ratio >= _SCALE_LIMIT:
        return [gapmod.cost_scale_suspect(f"{ratio:,.0f}x")]
    if ratio <= 1 / _SCALE_LIMIT:
        return [gapmod.cost_scale_suspect(f"1/{1 / ratio:,.0f} of")]
    return []


def _period_gaps(usage: Frame, cost: Frame | None) -> list:
    """A gap when the usage and cost files cover different days."""
    if cost is None or not usage.usage or not cost.cost:
        return []
    u = (usage.period_start, usage.period_end)
    c = (cost.period_start, cost.period_end)
    if None in u or None in c:
        return []
    if abs(u[0] - c[0]).days < 1 and abs(u[1] - c[1]).days < 1:
        return []

    def span(start, end):
        return f"{start:%Y-%m-%d} to {end:%Y-%m-%d}"

    return [gapmod.periods_differ(span(*u), span(*c))]


def _exit_code(frame: Frame | SpendReport) -> int:
    """1 when a gap means the headline total understates the real bill.

    Findings do not affect this: "we found something" is the tool working, not
    an error.
    """
    return 1 if any(g.invalidates_total for g in frame.gaps) else 0


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------


def _inspect(args) -> int:
    saved = _read_saved(args.usage)
    if saved is not None and is_analytics_envelope(saved):
        return _analytics(args, saved, mode="inspect")
    if saved is not None and is_claude_code_envelope(saved):
        return _claude_code(args, saved, mode="inspect")
    routed = _enterprise_profile(args)
    if routed is not None:
        return _enterprise(args, routed, mode="inspect")
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
        unmet = check.unmet(frame, CheckConfig())
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
    saved = _read_saved(args.usage)
    if saved is not None and is_analytics_envelope(saved):
        return _analytics(args, saved, mode="audit")
    if saved is not None and is_claude_code_envelope(saved):
        return _claude_code(args, saved, mode="audit")
    routed = _enterprise_profile(args)
    if routed is not None:
        return _enterprise(args, routed, mode="audit")
    try:
        frame, pricing, estimate = _load(args)
    except _LoadFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code

    run = run_checks(frame, pricing, _check_config(args))
    frame = frame.with_gaps(run.gaps)
    return _emit_audit(args, frame, pricing, estimate, run, checks_total=len(CHECKS))


def _check_config(args) -> CheckConfig:
    start, end = args.business_hours
    return CheckConfig(
        min_impact_usd=args.min_impact,
        spike_threshold=args.spike_threshold,
        utc_offset_hours=args.utc_offset,
        business_hour_start=start,
        business_hour_end=end,
        cache_prefix_share=args.cache_prefix_share,
        batch_eligible_share=args.batch_eligible_share,
    )


def _emit_audit(
    args,
    frame: Frame,
    pricing: PricingTable,
    estimate,
    run: CheckRun,
    *,
    checks_total: int,
    extra_json: dict | None = None,
) -> int:
    """Render a finished audit in the requested format, and return its exit code."""
    if args.json:
        payload = _as_json(frame, pricing, estimate)
        payload.update(extra_json or {})
        payload["findings"] = [_finding_json(f) for f in run.findings]
        payload["checks_blocked"] = [label for label, _ in run.skipped]
        payload["checks_clean"] = list(run.clean)
        print(json.dumps(payload, indent=2))
        return _exit_code(frame)

    if args.format == "markdown":
        # The shareable artifact. It carries its own provenance because it gets
        # forwarded away from the terminal that produced it.
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
                    f"{checks_total - len(run.skipped)} of {checks_total} checks.",
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
# fetch
# --------------------------------------------------------------------------


def _fetch(args) -> int:
    if args.api_key_env is None:
        args.api_key_env = _DEFAULT_KEY_ENV[args.source]
    if getattr(args, "rejected_api_key", None) is not None:
        # Deliberately does not echo the value back.
        print(
            "\n".join(
                [
                    "error: there is no --api-key flag, on purpose.",
                    "  A key passed as an argument lands in shell history and is",
                    "  visible to every user on the machine via `ps`.",
                    f"  Use an environment variable:  export "
                    f"{args.api_key_env}=sk-ant-admin01-...",
                ]
            ),
            file=sys.stderr,
        )
        return 2

    if args.api_key_env.startswith("sk-ant-"):
        # The key has been passed where the variable NAME belongs. Say so
        # without repeating it back into the terminal, a log, or CI output.
        print(
            "\n".join(
                [
                    "error: --api-key-env takes the NAME of an environment "
                    "variable, not the key itself.",
                    "  The value given looks like an API key, so it has not been "
                    "echoed here.",
                    "  Try:  export ANTHROPIC_ADMIN_KEY=<your key>   "
                    "# then run without --api-key-env",
                ]
            ),
            file=sys.stderr,
        )
        return 2

    key = os.environ.get(args.api_key_env, "").strip()
    analytics = args.source == "analytics"
    if not key:
        kind = "Analytics API key" if analytics else "Admin API key"
        example = "<your Analytics API key>" if analytics else "sk-ant-admin01-..."
        lines = [
            f"error: no {kind} found in ${args.api_key_env}.",
            f"  Set it first:  export {args.api_key_env}={example}",
            "",
            "  The key is deliberately not accepted as a command-line flag:",
            "  arguments land in shell history and are visible to every user",
            "  on the machine via `ps`.",
        ]
        print("\n".join(lines), file=sys.stderr)
        return 2

    if analytics:
        return _fetch_analytics(args, key)
    if args.source == "claude-code":
        return _fetch_claude_code(args, key)

    group_by = [g.strip() for g in (args.group_by or "").split(",") if g.strip()]
    client = ApiClient(Credential(key), max_retries=args.max_retries)

    try:
        if args.what == "cost":
            pages = client.cost(
                args.start,
                args.end,
                group_by=[g for g in group_by if g in ("description", "workspace_id")]
                or ["workspace_id", "description"],
                window=args.window,
            )
            bucket_width = "1d"
        else:
            pages = client.usage(
                args.start,
                args.end,
                bucket_width=args.bucket_width,
                group_by=group_by,
                window=args.window,
            )
            bucket_width = args.bucket_width
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    names = None if args.no_names else _lookup_names(client)

    payload = envelope(
        endpoint=args.what,
        starting_at=args.start,
        ending_at=args.end,
        bucket_width=bucket_width,
        group_by=group_by,
        pages=pages,
        version=__version__,
        names=names,
    )
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    buckets = sum(len(p.get("data", ())) for p in pages)
    # To stderr, so `fetch ... && audit ...` in a pipeline stays clean.
    print(
        f"wrote {args.output} -- {len(pages)} page(s), {buckets} time bucket(s).\n"
        f"Analyse it offline with:  claude-spend-audit audit {args.output}",
        file=sys.stderr,
    )
    return 0


def _lookup_names(client: ApiClient) -> dict[str, dict[str, str]] | None:
    """Workspace and API-key names, or None with a warning if unavailable.

    Optional by design: the usage or cost pages are the point of the fetch,
    so a key that may read usage but not list workspaces still gets its file.
    """
    try:
        return {
            "workspace_id": client.workspace_names(),
            "api_key_id": client.api_key_names(),
        }
    except ApiError as exc:
        print(
            f"warning: could not look up workspace and API-key names ({exc}). "
            "The file is saved without them; findings will show IDs.",
            file=sys.stderr,
        )
        return None


def _fetch_claude_code(args, key: str) -> int:
    """fetch --source claude-code: every day of the range, one file."""
    inapplicable = [
        flag
        for flag, used in (
            ("--what", args.what != "usage"),
            ("--bucket-width", args.bucket_width != "1d"),
            ("--group-by", args.group_by != ",".join(DEFAULT_FETCH_GROUP_BY)),
            ("--window", args.window),
            ("--no-users", args.no_users),
            ("--no-names", args.no_names),
        )
        if used
    ]
    if inapplicable:
        print(
            f"error: {', '.join(inapplicable)} does not apply to --source "
            "claude-code. The Claude Code Analytics API reports one fixed "
            "shape: a record per person per day.",
            file=sys.stderr,
        )
        return 2

    days = (args.end - args.start).days
    print(
        f"fetching {days} day(s) of Claude Code analytics -- one request per "
        "day, so this can take a while if the API asks us to slow down.",
        file=sys.stderr,
    )
    client = ApiClient(Credential(key), max_retries=args.max_retries)
    try:
        fetched = client.claude_code(
            args.start,
            args.end,
            progress=lambda day, pages: print(f"  {day}: {pages} page(s)", file=sys.stderr),
        )
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    payload = claude_code_envelope(
        starting_at=args.start, ending_at=args.end, days=fetched, version=__version__
    )
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    records = sum(len(p.get("data", ())) for d in fetched for p in d["pages"])
    print(
        f"wrote {args.output} -- {len(fetched)} day(s), {records} record(s).\n"
        "It holds users' email addresses; it stays on this machine unless you "
        "move it.\n"
        f"Analyse it offline with:  claude-spend-audit audit {args.output}",
        file=sys.stderr,
    )
    return 0


def _fetch_analytics(args, key: str) -> int:
    """fetch --source analytics: every series one audit needs, in one file."""
    if args.group_by == ",".join(DEFAULT_FETCH_GROUP_BY):
        # The parser's default is the Admin API's dimension list. Left alone,
        # it means "the default", which for this API is a different list.
        group_by = list(DEFAULT_ANALYTICS_GROUP_BY)
    else:
        group_by = [g.strip() for g in (args.group_by or "").split(",") if g.strip()]
    if "rbac_group_id" in group_by:
        print(
            "error: --group-by rbac_group_id is not supported for an audit. A "
            "user in several groups is counted in each group's row, so totals "
            "built from those rows would count the same tokens more than once.",
            file=sys.stderr,
        )
        return 2
    unknown = sorted(set(group_by) - ANALYTICS_USAGE_GROUP_BY)
    if unknown:
        print(
            f"error: unknown --group-by value(s) for the Analytics API: "
            f"{', '.join(unknown)}. Valid: {', '.join(sorted(ANALYTICS_USAGE_GROUP_BY))}",
            file=sys.stderr,
        )
        return 2

    client = ApiClient(Credential(key), max_retries=args.max_retries, key_kind="analytics")
    series: dict[str, dict] = {}
    try:
        series["usage"] = {
            "endpoint": ANALYTICS_USAGE_PATH,
            "bucket_width": args.bucket_width,
            "group_by": group_by,
            "pages": client.analytics_usage(
                args.start, args.end, bucket_width=args.bucket_width, group_by=group_by
            ),
        }
        if group_by:
            # The ungrouped total, to detect buckets the 100-group cap cut short.
            series["usage_total"] = {
                "endpoint": ANALYTICS_USAGE_PATH,
                "bucket_width": args.bucket_width,
                "group_by": [],
                "pages": client.analytics_usage(
                    args.start, args.end, bucket_width=args.bucket_width
                ),
            }
        # Ungrouped, so the billed total cannot be truncated by the group cap.
        series["cost_total"] = {
            "endpoint": ANALYTICS_COST_PATH,
            "bucket_width": "1d",
            "group_by": [],
            "pages": client.analytics_cost(args.start, args.end, bucket_width="1d"),
        }
        # Spend by RBAC group, for C7r. Group ids are not personal data, so
        # this is fetched even with --no-users. Never fed into the Frame: its
        # named-group rows overlap.
        series["groups_cost"] = {
            "endpoint": ANALYTICS_COST_PATH,
            "bucket_width": "1d",
            "group_by": ["rbac_group_id"],
            "pages": client.analytics_cost(
                args.start, args.end, bucket_width="1d", group_by=["rbac_group_id"]
            ),
        }
        if not args.no_users:
            per_user = ["product", "model"]
            series["user_usage"] = {
                "endpoint": ANALYTICS_USER_USAGE_PATH,
                "group_by": per_user,
                "pages": client.analytics_user_usage(args.start, args.end, group_by=per_user),
            }
            series["user_cost"] = {
                "endpoint": ANALYTICS_USER_COST_PATH,
                "group_by": per_user,
                "pages": client.analytics_user_cost(args.start, args.end, group_by=per_user),
            }
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    payload = analytics_envelope(
        starting_at=args.start,
        ending_at=args.end,
        series=series,
        version=__version__,
    )
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    pages = sum(len(entry["pages"]) for entry in series.values())
    print(
        f"wrote {args.output} -- {len(series)} series, {pages} page(s).\n"
        f"Analyse it offline with:  claude-spend-audit audit {args.output}",
        file=sys.stderr,
    )
    return 0


# --------------------------------------------------------------------------
# Claude Enterprise Analytics API (saved fetch)
# --------------------------------------------------------------------------


def _analytics(args, saved: dict, *, mode: str) -> int:
    """inspect or audit, for a file saved by `fetch --source analytics`."""
    if args.cost:
        print(
            "error: this analytics fetch already carries cost data. --cost "
            "applies only to Developer Console usage exports.",
            file=sys.stderr,
        )
        return 2
    try:
        pricing = (
            PricingTable.load(args.pricing) if args.pricing else PricingTable.load_default()
        )
    except PricingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        frame, report, coverage = load_analytics(saved, source=str(args.usage))
    except (AnalyticsFileError, ResponseShapeError, InvalidOperation) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not frame.cost:
        frame = frame.with_gaps([gapmod.no_cost_data()])
    frame, merged = pricing.merge_model_aliases(frame)
    frame = frame.with_gaps(merged)
    estimate = pricing.estimate_frame(frame)
    frame = frame.with_gaps(_analytics_gaps(estimate.gaps, frame))
    frame = frame.with_gaps(_scale_gaps(frame, estimate))
    if report is not None and args.anonymize:
        report = report.anonymized()

    extra = {"source": "enterprise_analytics", "per_user": report is not None}
    # C7r always counts: when the file lacks its series it is skipped, and
    # the report says so, rather than silently left out of the tally.
    checks_total = len(CHECKS) + 1 + (len(ENTERPRISE_CHECKS) if report is not None else 0)
    if coverage is None:
        group_gaps = [gapmod.rbac_groups_not_fetched()]
    elif coverage.capped_buckets:
        group_gaps = [gapmod.rbac_unassigned_row_missing(coverage.capped_buckets)]
    else:
        group_gaps = []

    if mode == "inspect":
        blocked: list[tuple[str, str]] = []
        for check in CHECKS:
            unmet = check.unmet(frame, CheckConfig())
            if unmet:
                frame = frame.with_gaps(unmet)
                blocked.append((check.label, "; ".join(g.message for g in unmet)))
        if report is not None:
            for check in ENTERPRISE_CHECKS:
                unmet = check.unmet(report)
                if unmet:
                    frame = frame.with_gaps(unmet)
                    blocked.append((check.label, "; ".join(g.message for g in unmet)))
        frame = frame.with_gaps(group_gaps)
        if coverage is None:
            blocked.append((rbac.LABEL, group_gaps[0].message))
        frame = replace(frame, gaps=tuple(_analytics_gaps(frame.gaps, frame)))
        if args.json:
            payload = _as_json(frame, pricing, estimate)
            payload.update(extra)
            payload["checks_blocked"] = [label for label, _ in blocked]
            print(json.dumps(payload, indent=2))
            return _exit_code(frame)
        out = terminal.render_header(frame, pricing, estimate, title="analytics summary")
        out.extend(terminal.render_dimensions(frame))
        out.extend(terminal.render_gaps(frame.gaps))
        out.extend(terminal.render_skipped(blocked))
        out.extend(
            terminal.footer(
                [
                    f"No checks run. {checks_total - len(blocked)} of {checks_total} "
                    "would run on this data -- use `audit`.",
                ]
            )
        )
        print("\n".join(out))
        return _exit_code(frame)

    run = run_checks(frame, pricing, _check_config(args))
    findings = list(run.findings)
    gaps = list(run.gaps)
    skipped = list(run.skipped)
    clean = list(run.clean)
    if report is not None:
        per_user = run_enterprise_checks(
            report, pricing, EnterpriseConfig(min_impact_usd=args.min_impact)
        )
        findings += per_user.findings
        gaps += per_user.gaps
        skipped += per_user.skipped
        clean += per_user.clean
    gaps += group_gaps
    if coverage is None:
        skipped.append((rbac.LABEL, group_gaps[0].message))
    else:
        produced = rbac.run_group_coverage(coverage, _check_config(args))
        findings += produced
        if not produced:
            clean.append(rbac.LABEL)
    combined = CheckRun(
        findings=tuple(rank(findings)),
        gaps=tuple(_analytics_gaps(gaps, frame)),
        clean=tuple(clean),
        skipped=tuple(skipped),
    )
    frame = frame.with_gaps(combined.gaps)
    return _emit_audit(
        args, frame, pricing, estimate, combined, checks_total=checks_total, extra_json=extra
    )


# --------------------------------------------------------------------------
# Claude Code Analytics API (saved fetch)
# --------------------------------------------------------------------------

#: Anthropic's estimate and ours may differ this much before the token
#: reading is called into question.
_CLAUDE_CODE_TOLERANCE = Decimal("0.10")


def _claude_code(args, saved: dict, *, mode: str) -> int:
    """inspect or audit, for a file saved by `fetch --source claude-code`."""
    from .enterprise.checks import E1, E3, E4

    if args.cost:
        print(
            "error: a Claude Code fetch carries its own estimated cost. --cost "
            "applies only to Developer Console usage exports.",
            file=sys.stderr,
        )
        return 2
    try:
        pricing = (
            PricingTable.load(args.pricing) if args.pricing else PricingTable.load_default()
        )
    except PricingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        data = load_claude_code(saved, source=str(args.usage))
    except (ClaudeCodeFileError, ResponseShapeError, InvalidOperation, KeyError, ValueError) as exc:
        print(f"error: the saved Claude Code fetch cannot be read: {exc}", file=sys.stderr)
        return 2

    frame, merged = pricing.merge_model_aliases(data.frame)
    frame = frame.with_gaps(merged)
    estimate = pricing.estimate_frame(frame)
    ours, theirs = estimate.usd, data.api_estimate_usd
    found = [
        gapmod.claude_code_estimates(_money(data.total_estimate_usd), _money(theirs), _money(ours))
    ]
    if theirs and abs(ours - theirs) / theirs > _CLAUDE_CODE_TOLERANCE:
        found.append(gapmod.claude_code_input_unconfirmed(_money(ours), _money(theirs)))
    frame = frame.with_gaps(found + list(data.gaps))
    frame = frame.with_gaps(_claude_code_gaps(estimate.gaps, frame))

    report, activity = data.report, data.activity
    if args.anonymize:
        labels = report.anonymous_labels()
        # Someone can appear in the activity records with no model usage at
        # all, so not in the SpendReport. They still need a label: an email
        # left in K1 would defeat --anonymize.
        unlabelled = sorted({r.actor for r in activity.records} - set(labels))
        width = max(3, len(str(len(labels) + len(unlabelled))))
        for offset, actor in enumerate(unlabelled, start=len(labels) + 1):
            labels[actor] = f"user-{offset:0{width}d}"
        report = report.anonymized()
        activity = activity.relabel(labels)

    per_user_checks = (E1, E3, E4)
    checks_total = len(CHECKS) + len(per_user_checks) + len(CODE_CHECKS)
    extra = {
        "source": "claude_code_analytics",
        "anthropic_estimate_usd": str(data.total_estimate_usd),
        "anthropic_estimate_api_usd": str(theirs),
    }

    if mode == "inspect":
        blocked: list[tuple[str, str]] = []
        for check in CHECKS:
            unmet = check.unmet(frame, CheckConfig())
            if unmet:
                frame = frame.with_gaps(unmet)
                blocked.append((check.label, "; ".join(g.message for g in unmet)))
        for check in per_user_checks:
            unmet = check.unmet(report)
            if unmet:
                blocked.append((check.label, "; ".join(g.message for g in unmet)))
        frame = replace(frame, gaps=tuple(_claude_code_gaps(frame.gaps, frame)))
        if args.json:
            payload = _as_json(frame, pricing, estimate)
            payload.update(extra)
            payload["checks_blocked"] = [label for label, _ in blocked]
            print(json.dumps(payload, indent=2))
            return _exit_code(frame)
        out = terminal.render_header(frame, pricing, estimate, title="Claude Code summary")
        out.extend(terminal.render_dimensions(frame))
        out.extend(terminal.render_gaps(frame.gaps))
        out.extend(terminal.render_skipped(blocked))
        out.extend(
            terminal.footer(
                [
                    f"No checks run. {checks_total - len(blocked)} of {checks_total} "
                    "would run on this data -- use `audit`.",
                ]
            )
        )
        print("\n".join(out))
        return _exit_code(frame)

    run = run_checks(frame, pricing, _check_config(args))
    per_user = run_enterprise_checks(
        report, pricing, EnterpriseConfig(min_impact_usd=args.min_impact),
        checks=per_user_checks,
    )
    code = run_code_checks(activity, CodeConfig(min_impact_usd=args.min_impact))
    combined = CheckRun(
        findings=tuple(rank([*run.findings, *per_user.findings, *code.findings])),
        gaps=tuple(_claude_code_gaps([*run.gaps, *per_user.gaps], frame)),
        clean=(*run.clean, *per_user.clean, *code.clean),
        skipped=(*run.skipped, *per_user.skipped),
    )
    frame = frame.with_gaps(combined.gaps)
    return _emit_audit(
        args, frame, pricing, estimate, combined, checks_total=checks_total, extra_json=extra
    )


def _money(amount: Decimal) -> str:
    return f"${amount:,.2f}"


def _claude_code_gaps(gaps, frame: Frame) -> list:
    """Gaps for a Claude Code audit, without advice that cannot be followed.

    The endpoint has one fixed shape, broken out by model only. "Re-export
    grouped by workspace" or "re-export hourly" would send the reader after
    something that does not exist, so those gaps are dropped in favour of one
    that says which checks this source can never support; the pricing gap
    says what was assumed instead of how to re-export.
    """
    kept = [gapmod.claude_code_shape()]
    for gap in gaps:
        if gap.code in (GapCode.DIMENSION_NOT_GROUPED, GapCode.GRANULARITY_TOO_COARSE):
            continue
        if gap.code is GapCode.PRICING_DIMENSION_NOT_GROUPED:
            missing = tuple(d for d in PRICE_DIMENSIONS if not frame.has_dimension(d))
            if missing:
                kept.append(gapmod.claude_code_pricing_basis(missing))
            continue
        if gap.code is GapCode.CACHE_TTL_NOT_SPLIT:
            # The generic remedy points at a cookbook CSV; here the API itself
            # is the source of the summed figure, and nothing splits it. Nor
            # does it understate a bill: this source has no billed total, and
            # the headline is Anthropic's estimate, which knows the TTLs. Left
            # as invalidating, every Claude Code audit would exit 1.
            gap = replace(
                gap,
                invalidates_total=False,
                remedy=(
                    "Nothing on this source splits them: the Claude Code "
                    "Analytics API reports cache writes as one figure. "
                    "Anthropic's own estimate, in the caveats, prices them "
                    "with the TTL it knows."
                ),
            )
        kept.append(gap)
    return kept


#: Dimensions the Analytics API never reports. A gap telling the reader to
#: "re-export grouped by" one of them would send them after something that
#: does not exist; the plan-context gap explains their absence instead.
_ANALYTICS_ABSENT = frozenset({"service_tier", "workspace_id", "api_key_id"})


def _analytics_gaps(gaps, frame: Frame) -> list:
    """Gaps for an analytics audit, without the ones that do not apply.

    The usage path's "not grouped by" gaps assume a re-export could add the
    dimension. Here it cannot, so those are dropped for the dimensions this
    API lacks, and the pricing-dimension gap is rebuilt without service_tier
    -- it would otherwise warn that batch spend might be overstated, on data
    where batch does not exist.
    """
    kept = []
    for gap in gaps:
        if gap.code is GapCode.DIMENSION_NOT_GROUPED and gap.scope in _ANALYTICS_ABSENT:
            continue
        if gap.code is GapCode.PRICING_DIMENSION_NOT_GROUPED:
            missing = tuple(
                d for d in PRICE_DIMENSIONS
                if d not in _ANALYTICS_ABSENT and not frame.has_dimension(d)
            )
            if missing:
                kept.append(gapmod.pricing_dimension_not_grouped(missing))
            continue
        kept.append(gap)
    return kept


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
        "gaps": _gaps_json(frame.gaps),
    }


# --------------------------------------------------------------------------
# Team/Enterprise spend report
# --------------------------------------------------------------------------


def _enterprise_profile(args) -> Profile | None:
    """The spend-report profile to route this run to, or None for usage data.

    An explicit --usage-profile of the enterprise kind routes; any other
    explicit profile does not. Otherwise the file decides by its columns, and
    only a file no usage profile matches can be claimed -- so a Console or
    cookbook export can never be taken away from the usage path.
    """
    if args.usage_profile:
        try:
            explicit = Profile.load(args.usage_profile)
        except SchemaError:
            return None  # the usage path reports the profile error itself
        return explicit if explicit.kind == ENTERPRISE_KIND else None
    if _read_saved(args.usage) is not None:
        return None
    return detect_enterprise(args.usage)


def _enterprise(args, profile: Profile, *, mode: str) -> int:
    """inspect or audit, for a Team/Enterprise spend report."""
    if args.cost:
        print(
            "error: this is a Team/Enterprise spend report, which already "
            "carries spend. --cost applies only to Developer Console usage "
            "exports.",
            file=sys.stderr,
        )
        return 2
    try:
        pricing = (
            PricingTable.load(args.pricing) if args.pricing else PricingTable.load_default()
        )
    except PricingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        report, mapping = load_enterprise_csv(
            args.usage, profile, date_order=getattr(args, "date_order", None)
        )
    except SchemaMismatch as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (SchemaError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.explain_schema:
        print(mapping.explain(profile))
        print()
    if args.anonymize:
        report = report.anonymized()

    if mode == "inspect":
        blocked: list[tuple[str, str]] = []
        for check in ENTERPRISE_CHECKS:
            unmet = check.unmet(report)
            if unmet:
                report = report.with_gaps(unmet)
                blocked.append((check.label, "; ".join(g.message for g in unmet)))
        if args.json:
            payload = _enterprise_json(report, pricing)
            payload["checks_blocked"] = [label for label, _ in blocked]
            print(json.dumps(payload, indent=2))
            return _exit_code(report)
        out = terminal.render_enterprise_header(report, pricing, title="spend report summary")
        out.extend(terminal.render_gaps(report.gaps))
        out.extend(terminal.render_skipped(blocked))
        out.extend(
            terminal.footer(
                [
                    f"No checks run. {len(ENTERPRISE_CHECKS) - len(blocked)} of "
                    f"{len(ENTERPRISE_CHECKS)} would run on this data -- use `audit`.",
                ]
            )
        )
        print("\n".join(out))
        return _exit_code(report)

    run = run_enterprise_checks(
        report, pricing, EnterpriseConfig(min_impact_usd=args.min_impact)
    )
    report = report.with_gaps(run.gaps)

    if args.json:
        payload = _enterprise_json(report, pricing)
        payload["findings"] = [_finding_json(f) for f in run.findings]
        payload["checks_blocked"] = [label for label, _ in run.skipped]
        payload["checks_clean"] = list(run.clean)
        print(json.dumps(payload, indent=2))
        return _exit_code(report)

    if args.format == "markdown":
        text = markdown.render_enterprise(
            report, pricing, run.findings, skipped=run.skipped, version=__version__
        )
    else:
        out = terminal.render_enterprise_header(report, pricing, title="spend report audit")
        out.extend(terminal.render_findings(run.findings))
        out.extend(terminal.render_skipped(run.skipped))
        out.extend(terminal.render_gaps(report.gaps))
        out.extend(
            terminal.footer(
                [
                    f"{len(run.findings)} descriptive finding(s) from "
                    f"{len(ENTERPRISE_CHECKS) - len(run.skipped)} of "
                    f"{len(ENTERPRISE_CHECKS)} checks. None claims a saving.",
                ]
            )
        )
        text = "\n".join(out)

    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(text)
    return _exit_code(report)


def _enterprise_json(report: SpendReport, pricing: PricingTable) -> dict:
    net = report.total("net_spend_usd")
    gross = report.total("gross_spend_usd")
    return {
        "report_type": ENTERPRISE_KIND,
        # Stated in the payload so a consumer cannot mistake an observation
        # for a savings figure: no finding in this report claims one.
        "descriptive_only": True,
        "period": {
            "start": report.period_start.isoformat() if report.period_start else None,
            "end": report.period_end.isoformat() if report.period_end else None,
        },
        "pricing": {
            "version": pricing.version,
            "fetched_at": pricing.fetched_at,
            "source_url": pricing.source_url,
            "used_for": "model tiers and list prices only",
        },
        "sources": [
            {
                "path": s.path,
                "ingest_path": s.ingest_path,
                "profile": s.profile,
                "profile_status": s.profile_status,
                "unmapped_columns": list(s.unmapped_columns),
            }
            for s in report.sources
        ],
        "spend": {
            # Money as strings: a JSON float would round it.
            "net_usd": str(net) if net is not None else None,
            "gross_usd": str(gross) if gross is not None else None,
            "analysed_column": report.spend_field,
        },
        "totals": {
            name: (report.total(name) if report.has(name) else None) for name in INT_FIELDS
        },
        "counts": {
            "rows": len(report.rows),
            "users": len(report.distinct("user_email")) if report.has("user_email") else None,
            "products": len(report.distinct("product")) if report.has("product") else None,
            "models": len(report.distinct("model")) if report.has("model") else None,
        },
        "present_fields": sorted(report.present),
        "gaps": _gaps_json(report.gaps),
    }


def _gaps_json(gaps) -> list[dict]:
    return [
        {
            "code": str(g.code),
            "severity": str(g.severity),
            "scope": g.scope,
            "message": g.message,
            "invalidates_total": g.invalidates_total,
            "remedy": g.remedy,
        }
        for g in sorted(gaps, key=lambda g: _SEVERITY_ORDER[str(g.severity)])
    ]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
