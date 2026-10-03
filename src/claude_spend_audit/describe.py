"""Describe a file's format without revealing its contents.

No real export has been seen for most input paths, so mismatches are
expected -- and the person who hits one cannot be asked to send their data.
This module prints everything needed to fix a mapping and nothing else:

* which path the file would take, and which schema profile matched;
* each column, the field it maps to, and the *shape* of its values
  ("date 9999-99-99", "integer with , thousands") -- never the values;
* for a saved fetch, the field names and JSON types of every series;
* what the loader would do with it: load (with which caveat codes) or
  refuse (with the reason, any cell value replaced by its shape).

The only values ever printed are short codes from fields whose values are an
API enumeration or a model ID -- ``currency``, ``service_tier``, ``model``
and the like -- because those are not about anyone, and they are often the
thing that is mismatched. Names, emails, workspaces, keys and every number
stay out.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

from .ingest.api_client import is_analytics_envelope, is_envelope
from .ingest.schema import (
    Profile,
    SchemaError,
    SchemaMismatch,
    ValueMismatch,
    is_totals_label,
    select_profile,
    unit_conflicts,
)

#: Fields whose values are safe to print: API enumerations and model IDs.
_ENUM_FIELDS = frozenset(
    {
        "currency",
        "service_tier",
        "context_window",
        "inference_geo",
        "speed",
        "cost_type",
        "token_type",
        "model",
        "model_family",
        "product",
    }
)
_ENUM_VALUE = re.compile(r"^[A-Za-z0-9 ._\-]{1,40}$")
_MAX_ENUM_VALUES = 12

_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern))
    for name, pattern in (
        ("date 9999-99-99", r"^\d{4}-\d{2}-\d{2}$"),
        ("timestamp 9999-99-99T99:99:99Z", r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$"),
        ("timestamp 9999-99-99 99:99", r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?( ?(UTC|GMT))?$"),
        ("slash date 99/99/9999", r"^\d{1,2}/\d{1,2}/\d{4}( .*)?$"),
        ("slash date 9999/99/99", r"^\d{4}/\d{1,2}/\d{1,2}$"),
        ("integer", r"^-?\d+$"),
        ("integer with , thousands", r"^-?\d{1,3}(,\d{3})+$"),
        ("decimal", r"^-?\d+\.\d+$"),
        ("decimal with , thousands", r"^-?\d{1,3}(,\d{3})+\.\d+$"),
        ("$ amount", r"^-?\$-?[\d,]+(\.\d+)?$"),
        ("amount in parentheses", r"^\(\$?[\d,]+(\.\d+)?\)$"),
        ("decimal comma", r"^-?\d+(\.\d{3})*,\d+$"),
        ("scientific notation", r"^-?\d+(\.\d+)?[eE][+-]?\d+$"),
        ("abbreviated number (K/M/B)", r"^-?\d+(\.\d+)?\s?[KkMmBb]$"),
        ("email-like", r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    )
)


def shape_of(value: str) -> str:
    """The shape of one cell, never its content."""
    text = value.strip()
    if not text:
        return "empty"
    if is_totals_label(text):
        return "totals label"
    for name, pattern in _SHAPES:
        if pattern.match(text):
            return name
    if text.lower() in ("nan", "inf", "infinity", "-inf", "null", "none", "n/a", "-"):
        return "placeholder (nan/null/n-a/-)"
    return "text"


def describe(path: str | Path, *, date_order: str | None = None) -> str:
    """The whole description of one file, as printable text."""
    p = Path(path)
    lines = [f"describe: {p.name}", ""]
    if not p.exists():
        return "\n".join(lines + [f"  no such file: {p}"])

    payload = _json(p)
    if payload is not None:
        lines += _describe_json(payload)
    else:
        lines += _describe_csv(p, date_order)

    lines += [
        "",
        "Nothing above is a figure, name, email, workspace or key from the file.",
        "If this file was refused or mis-read, paste this output into a",
        "'format mismatch' issue. Never paste the file itself.",
    ]
    # Profile paths in error messages would name the user's home directory.
    return "\n".join(lines).replace(str(Path.home()), "~")


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------


def _describe_csv(path: Path, date_order: str | None) -> list[str]:
    from .ingest.csv_ingest import _cell, _read

    try:
        headers, records = _read(path)
    except (SchemaMismatch, OSError) as exc:
        return ["  could not be read as CSV:", *_indent(_redacted(exc))]

    out = [f"  CSV, {len(headers)} column(s), {len(records)} data row(s)", ""]

    matches: dict[str, Profile | None] = {}
    out.append("  profile matching:")
    for kind in ("usage", "cost", "enterprise_spend"):
        try:
            profile = select_profile(headers, kind)
            matches[kind] = profile
            note = ""
            if kind == "cost":
                conflicts = unit_conflicts(headers, profile)
                if conflicts:
                    note = (
                        f"; also fits {', '.join(c.name for c in conflicts)}, "
                        "which reads amounts in different units"
                    )
            out.append(
                f"    {kind:<17} {profile.name} [{profile.status}], amounts in "
                f"{profile.amount_units}{note}"
                if kind == "cost"
                else f"    {kind:<17} {profile.name} [{profile.status}]"
            )
        except (SchemaMismatch, SchemaError) as exc:
            matches[kind] = None
            out.append(f"    {kind:<17} no match")
            out += [f"      {line.strip()}" for line in _reason_lines(exc)]

    from .ingest.enterprise_csv import detect_enterprise

    enterprise = detect_enterprise(path)
    if enterprise is not None:
        route, profile = "Team/Enterprise spend report", enterprise
    elif matches["usage"] is not None:
        route, profile = "usage export", matches["usage"]
    elif matches["cost"] is not None:
        route, profile = "cost export (pass it with --cost)", matches["cost"]
    else:
        route, profile = None, None
    out += ["", f"  would be read as: {route or 'nothing -- no profile matches'}", ""]

    mapping = None
    if profile is not None:
        try:
            mapping = profile.match(headers)
        except SchemaMismatch:
            mapping = None
    field_of = {i: name for name, i in mapping.fields.items()} if mapping else {}

    out.append("  columns:")
    for i, header in enumerate(headers):
        values = [_cell(r, i) for r in records]
        field = field_of.get(i)
        target = f"-> {field}" if field else "-> (not used)"
        out.append(f"    {header[:40]!r:<44} {target}")
        out.append(f"        {_shape_summary(values)}")
        if field in _ENUM_FIELDS:
            safe = sorted({v.strip() for v in values if v.strip() and _ENUM_VALUE.match(v.strip())})
            if safe:
                shown = ", ".join(safe[:_MAX_ENUM_VALUES])
                more = f" (+{len(safe) - _MAX_ENUM_VALUES} more)" if len(safe) > _MAX_ENUM_VALUES else ""
                out.append(f"        values: {shown}{more}")

    out += ["", "  dry run:"]
    out += _indent(_dry_run_csv(path, route, profile, date_order), "    ")
    return out


def _shape_summary(values: Iterable[str]) -> str:
    values = list(values)
    shapes = Counter(shape_of(v) for v in values)
    distinct = len({v.strip() for v in values if v.strip()})
    parts = [f"{name} x{count}" for name, count in shapes.most_common()]
    return f"{', '.join(parts)}; {distinct} distinct"


def _dry_run_csv(path: Path, route: str | None, profile: Profile | None, date_order) -> list[str]:
    if route is None or profile is None:
        return ["not loaded: no profile matches"]
    from .ingest import load_cost_csv, load_usage_csv
    from .ingest.enterprise_csv import load_enterprise_csv

    try:
        if route.startswith("Team"):
            result, _ = load_enterprise_csv(path, profile, date_order=date_order)
            rows = len(result.rows)
        elif route.startswith("usage"):
            result, _ = load_usage_csv(path, date_order=date_order)
            rows = len(result.usage)
        else:
            result, _ = load_cost_csv(path, date_order=date_order)
            rows = len(result.cost)
    except (SchemaMismatch, SchemaError) as exc:
        return ["refused:", *_indent(_redacted(exc))]
    codes = sorted({g.code.value for g in result.gaps})
    return [f"loads {rows} row(s)", f"caveat codes: {', '.join(codes) or 'none'}"]


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------


def _json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError):
        return None


def _describe_json(payload: Any) -> list[str]:
    if is_analytics_envelope(payload):
        out = ["  saved fetch: Claude Enterprise Analytics API", ""]
        out += _request_lines(payload)
        for name, series in (payload.get("series") or {}).items():
            out += ["", f"  series {name!r}: {series.get('endpoint')}"]
            out.append(
                f"    bucket_width={series.get('bucket_width')}, "
                f"group_by={list(series.get('group_by') or [])}"
            )
            out += _indent(_page_structure(series.get("pages") or []), "    ")
        out += ["", "  dry run:", *_indent(_dry_run_analytics(payload), "    ")]
        return out
    if is_envelope(payload):
        out = [f"  saved fetch: Usage & Cost Admin API, endpoint {payload.get('endpoint')!r}", ""]
        out += _request_lines(payload)
        names = payload.get("names") or {}
        if isinstance(names, Mapping):
            # Counts only: names are often a person's or a project's.
            out.append(
                f"  names: {len(names.get('workspace_id') or {})} workspace(s), "
                f"{len(names.get('api_key_id') or {})} API key(s)"
            )
        out += _indent(_page_structure(payload.get("pages") or []), "  ")
        out += ["", "  dry run:", *_indent(_dry_run_admin(payload), "    ")]
        return out
    out = ["  JSON, but not a file saved by `fetch`. Its structure:"]
    out += _indent(_structure_lines(payload), "    ")
    return out


def _request_lines(payload: Mapping[str, Any]) -> list[str]:
    request = payload.get("request") or {}
    return [
        f"  written by version {payload.get('version')}",
        f"  request keys: {', '.join(sorted(request))}",
    ]


def _page_structure(pages: list) -> list[str]:
    buckets = [b for p in pages if isinstance(p, Mapping) for b in p.get("data") or []]
    rows = [r for b in buckets if isinstance(b, Mapping) for r in b.get("results") or []]
    page_keys: dict[str, set[str]] = {}
    for page in pages:
        if isinstance(page, Mapping):
            _collect(page, "", page_keys, skip=("data",))
    page_keys.pop("(top level)", None)
    lines = [f"{len(pages)} page(s), {len(buckets)} data item(s), {len(rows)} result row(s)"]
    lines.append("page fields:")
    lines += [f"  {k}: {', '.join(sorted(t))}" for k, t in sorted(page_keys.items())]
    item_keys: dict[str, set[str]] = {}
    for item in buckets:
        if isinstance(item, Mapping):
            _collect(item, "", item_keys)
    item_keys.pop("(top level)", None)
    if item_keys:
        lines.append("data item fields:")
        lines += [f"  {k}: {', '.join(sorted(t))}" for k, t in sorted(item_keys.items())]
    return lines


def _structure_lines(payload: Any) -> list[str]:
    keys: dict[str, set[str]] = {}
    _collect(payload, "", keys)
    return [f"{k or '(top level)'}: {', '.join(sorted(t))}" for k, t in sorted(keys.items())]


def _collect(value: Any, path: str, into: dict[str, set[str]], skip: tuple[str, ...] = ()) -> None:
    """Field paths and JSON types, never values. List items are walked as ``[]``."""
    into.setdefault(path or "(top level)", set()).add(_type(value))
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            if key in skip:
                into.setdefault(child, set()).add(_type(item))
            else:
                _collect(item, child, into)
    elif isinstance(value, list):
        for item in value:
            _collect(item, f"{path}[]", into)


def _type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        shape = shape_of(value)
        return f"string ({shape})" if shape != "text" else "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _dry_run_analytics(payload: Mapping[str, Any]) -> list[str]:
    from .ingest.analytics_json import AnalyticsFileError, load_analytics
    from .ingest.api_json import ResponseShapeError

    try:
        frame, report, coverage = load_analytics(payload)
    except (AnalyticsFileError, ResponseShapeError, ValueError, KeyError, TypeError) as exc:
        return ["refused:", *_indent(_redacted(exc))]
    codes = sorted({g.code.value for g in frame.gaps})
    if report is not None:
        codes = sorted(set(codes) | {g.code.value for g in report.gaps})
    return [
        f"loads {len(frame.usage)} usage row(s), {len(frame.cost)} cost row(s), "
        f"{len(report.rows) if report else 0} per-user row(s), "
        f"RBAC coverage {'present' if coverage else 'absent'}",
        f"caveat codes: {', '.join(codes) or 'none'}",
    ]


def _dry_run_admin(payload: Mapping[str, Any]) -> list[str]:
    from .ingest.api_json import ResponseShapeError, load_cost_json, load_usage_json

    request = payload.get("request") or {}
    pages = payload.get("pages") or []
    try:
        if payload.get("endpoint") == "cost":
            frame = load_cost_json(pages)
            rows = len(frame.cost)
        else:
            frame = load_usage_json(pages, group_by=request.get("group_by") or ())
            rows = len(frame.usage)
    except (ResponseShapeError, ValueError, KeyError, TypeError) as exc:
        return ["refused:", *_indent(_redacted(exc))]
    codes = sorted({g.code.value for g in frame.gaps})
    return [f"loads {rows} row(s)", f"caveat codes: {', '.join(codes) or 'none'}"]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _redacted(exc: BaseException) -> list[str]:
    """An error message with any offending cell replaced by its shape.

    The header list a SchemaMismatch prints is kept: headers are the format,
    not the data.
    """
    message = str(exc)
    raw = getattr(exc, "raw", None) if isinstance(exc, ValueMismatch) else None
    if raw is not None:
        for form in {repr(raw), repr(raw.strip())}:
            message = message.replace(form, f"<{shape_of(raw)}>")
    return message.splitlines()


def _reason_lines(exc: BaseException) -> list[str]:
    """The summary of a mismatch: its message up to the header listing."""
    lines = []
    for line in str(exc).splitlines():
        if not line.strip():
            break
        lines.append(line)
    return lines or [type(exc).__name__]


def _indent(lines: list[str], pad: str = "  ") -> list[str]:
    return [f"{pad}{line}" if line else line for line in lines]
