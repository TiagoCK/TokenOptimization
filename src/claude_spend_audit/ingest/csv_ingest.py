"""CSV ingest for Console Usage and Cost page exports.

The path anyone can use: no admin key, no security review, thirty seconds of
work.

All header interpretation is delegated to a schema profile. This module decides
nothing about column names.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from datetime import timedelta
from pathlib import Path

from .. import gaps as gapmod
from ..frame import (
    ALL_TOKEN_FIELDS,
    DIMENSIONS,
    TOKEN_FIELDS,
    UNSPLIT_CACHE_WRITE,
    CostRow,
    Frame,
    Segment,
    SourceInfo,
    TokenBuckets,
    UsageRow,
)
from .schema import (
    BUCKET_DELTAS,
    AmbiguousDate,
    Mapping_,
    Profile,
    SchemaMismatch,
    ValueMismatch,
    infer_bucket_width,
    is_totals_label,
    normalize,
    resolve_slash_order,
    select_profile,
    unit_conflicts,
)

#: Markers that would positively indicate Bedrock-routed traffic. Absent one of
#: these there is no signal in aggregate data, so no claim is made.
_BEDROCK_MARKERS = ("bedrock", "aws marketplace", "anthropic.claude-")

#: Fallback bucket length when an export has no end column and only one bucket.
_DEFAULT_BUCKET = timedelta(days=1)


def load_usage_csv(
    path: str | Path,
    profile: Profile | None = None,
    *,
    date_order: str | None = None,
) -> tuple[Frame, Mapping_]:
    """Read a Usage page export into a Frame.

    ``date_order`` ("mdy" or "dmy") settles slash dates the file itself
    cannot; see schema.resolve_slash_order.
    """
    p = Path(path)
    headers, records = _read(p)
    if profile is None:
        # Auto-select: more than one producer writes these files, and they
        # disagree on column names and on cents-vs-dollars.
        profile = select_profile(headers, "usage")
    if profile.kind != "usage":
        raise SchemaMismatch(
            f"Profile {profile.name!r} describes {profile.kind!r} data, not usage.",
            headers=(),
            profile=profile.name,
            profile_path=profile.path,
        )
    mapping = profile.match(headers)
    parsed, skipped = _parse_rows(profile, mapping, records, date_order)

    starts = [values["start"] for values, _ in parsed]
    width = infer_bucket_width(starts)
    rows: list[UsageRow] = []
    bedrock_seen = False
    for values, record in parsed:
        start = values["start"]
        rows.append(
            UsageRow(
                start=start,
                end=values.get("end") or start + _bucket(width),
                segment=Segment(
                    **{d: values.get(d) for d in DIMENSIONS if d in mapping.fields}
                ),
                tokens=TokenBuckets(
                    **{f: values.get(f) or 0 for f in ALL_TOKEN_FIELDS}
                ),
                web_search_requests=values.get("web_search_requests") or 0,
            )
        )
        if not bedrock_seen and _looks_like_bedrock(record):
            bedrock_seen = True

    # A dimension counts as grouped only if the export carries the column at
    # all. See Frame.grouped_by -- this is what stops C7 inventing findings.
    # Token buckets follow the same rule; see Frame.token_fields.
    grouped = frozenset(d for d in DIMENSIONS if d in mapping.fields)
    token_fields = frozenset(f for f in ALL_TOKEN_FIELDS if f in mapping.fields)

    frame = Frame(
        usage=tuple(rows),
        grouped_by=grouped,
        bucket_width=width,
        token_fields=token_fields,
        sources=(
            SourceInfo(
                path=str(p),
                ingest_path="csv",
                profile=profile.name,
                profile_status=profile.status,
                unmapped_columns=mapping.unmapped,
            ),
        ),
    )
    found = _usage_gaps(profile, mapping, frame, bedrock_seen) + skipped
    return frame.with_gaps(found), mapping


def load_cost_csv(
    path: str | Path,
    profile: Profile | None = None,
    *,
    date_order: str | None = None,
) -> tuple[Frame, Mapping_]:
    """Read a Cost page export into a Frame."""
    p = Path(path)
    headers, records = _read(p)
    inferred_units = None
    if profile is None:
        profile = select_profile(headers, "cost")
        conflicts = unit_conflicts(headers, profile)
        if conflicts:
            inferred_units = gapmod.cost_units_inferred(
                profile.name, profile.amount_units, conflicts[0].amount_units
            )
    if profile.kind != "cost":
        raise SchemaMismatch(
            f"Profile {profile.name!r} describes {profile.kind!r} data, not cost.",
            headers=(),
            profile=profile.name,
            profile_path=profile.path,
        )
    mapping = profile.match(headers)
    parsed, skipped = _parse_rows(profile, mapping, records, date_order)

    # "123.45" in USD means $1.23 -- the API reports the lowest currency unit.
    # Converting once, here, is the only place this factor appears.
    divisor = Decimal("100") if profile.amount_units == "cents" else Decimal("1")

    width = infer_bucket_width([values["start"] for values, _ in parsed])
    rows: list[CostRow] = []
    bedrock_seen = False
    for values, record in parsed:
        start = values["start"]
        currency = values.get("currency") or "USD"
        _require_usd(currency, profile)
        rows.append(
            CostRow(
                start=start,
                end=values.get("end") or start + _bucket(width),
                amount_usd=(values.get("amount") or Decimal("0")) / divisor,
                currency=currency,
                cost_type=values.get("cost_type"),
                description=values.get("description"),
                token_type=values.get("token_type"),
                workspace_id=values.get("workspace_id"),
                model=values.get("model"),
                service_tier=values.get("service_tier"),
                context_window=values.get("context_window"),
                inference_geo=values.get("inference_geo"),
            )
        )
        if not bedrock_seen and _looks_like_bedrock(record):
            bedrock_seen = True

    frame = Frame(
        cost=tuple(rows),
        sources=(
            SourceInfo(
                path=str(p),
                ingest_path="csv",
                profile=profile.name,
                profile_status=profile.status,
                unmapped_columns=mapping.unmapped,
            ),
        ),
    )
    found = _cost_gaps(profile, mapping, frame, bedrock_seen) + skipped
    if inferred_units is not None:
        found.append(inferred_units)
    return frame.with_gaps(found), mapping


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------

#: Byte-order marks that identify a UTF-16 file, as Excel's "Unicode Text"
#: format writes it.
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def _read(path: Path) -> tuple[tuple[str, ...], list[list[str]]]:
    if not path.exists():
        raise FileNotFoundError(f"no such file: {path}")
    raw = path.read_bytes()
    if raw.startswith(_UTF16_BOMS):
        text = raw.decode("utf-16")
    else:
        try:
            # utf-8-sig: Console exports opened in Excel routinely acquire a
            # BOM, which would otherwise corrupt the first header.
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            # No guessing at a legacy code page: a wrong guess garbles names
            # quietly, where this error costs one re-save.
            raise ValueMismatch(
                f"{path} is not UTF-8 text (byte {exc.start} cannot be read).",
                profile="?",
                hint=(
                    "Re-save it from your spreadsheet as 'CSV UTF-8 (comma "
                    "delimited)', or export it again from the Console."
                ),
            ) from exc
    lines = text.splitlines()
    first = lines[0] if lines else ""
    # Comma unless the header line plainly uses something else: European
    # Excel writes semicolons, and "Unicode Text" writes tabs.
    delimiter = ","
    if "," not in first:
        if ";" in first:
            delimiter = ";"
        elif "\t" in first:
            delimiter = "\t"
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise SchemaMismatch(
            f"{path} is empty.", headers=(), profile="?", profile_path=None
        )
    return tuple(rows[0]), rows[1:]


def _parse_rows(
    profile: Profile,
    mapping: Mapping_,
    records: list[list[str]],
    date_order: str | None,
) -> tuple[list[tuple[dict, list[str]]], list]:
    """Parse every data row, setting aside summary and dateless rows.

    Returns the parsed rows (each with its raw record) and the gaps that
    report what was set aside. Nothing is dropped without one.
    """
    start_idx = mapping.fields["start"]
    date_cols = [mapping.fields[n] for n in ("start", "end") if n in mapping.fields]

    kept_records: list[tuple[int, list[str]]] = []
    totals = 0
    for row_no, record in enumerate(records, start=2):
        first = next((cell for cell in record if cell.strip()), "")
        if is_totals_label(_cell(record, start_idx)) or is_totals_label(first):
            totals += 1
            continue
        kept_records.append((row_no, record))

    try:
        order = resolve_slash_order(
            (_cell(record, i) for _, record in kept_records for i in date_cols),
            date_order,
        )
    except AmbiguousDate as exc:
        raise ValueMismatch(
            f"Cannot read this file's dates: {exc}.",
            profile=profile.name,
            hint="Pass --date-order mdy or --date-order dmy.",
        ) from exc

    parsed: list[tuple[dict, list[str]]] = []
    dateless = 0
    dateless_tokens = 0
    dateless_amount = Decimal("0")
    for row_no, record in kept_records:
        values = {
            name: profile.parse(
                profile.columns[name], _cell(record, idx), row_no=row_no, date_order=order
            )
            for name, idx in mapping.fields.items()
        }
        if values.get("start") is None:
            tokens = sum(values.get(f) or 0 for f in ALL_TOKEN_FIELDS)
            amount = values.get("amount") or Decimal("0")
            if tokens or amount:
                dateless += 1
                dateless_tokens += tokens
                dateless_amount += amount
            continue  # a wholly empty row carries nothing to lose
        parsed.append((values, record))

    gaps = []
    if totals:
        gaps.append(gapmod.csv_rows_excluded(totals, "are labelled as totals"))
    if dateless:
        detail = (
            f"{dateless_tokens:,} tokens" if dateless_tokens else f"amount {dateless_amount:,}"
        )
        gaps.append(gapmod.csv_rows_excluded(dateless, "have no date", detail))
    return parsed, gaps


def _bucket(width: str | None) -> timedelta:
    """How long a bucket lasts, for rows the export gave no end time."""
    return timedelta(seconds=BUCKET_DELTAS[width]) if width else _DEFAULT_BUCKET


def _require_usd(currency: str, profile: Profile) -> None:
    if currency.strip().upper() != "USD":
        raise ValueMismatch(
            f"This file reports amounts in {currency!r}. Every figure this tool "
            "produces is in US dollars, and it does not convert currencies.",
            profile=profile.name,
            hint="Export the report in USD if your account allows it.",
        )


def _cell(record: list[str], index: int) -> str:
    return record[index] if index < len(record) else ""


def _looks_like_bedrock(record: list[str]) -> bool:
    joined = " ".join(record).lower()
    return any(marker in joined for marker in _BEDROCK_MARKERS)


def _source_gaps(profile: Profile, mapping: Mapping_, bedrock_seen: bool) -> list:
    """Gaps about the file itself, common to both kinds of export."""
    found = []
    if profile.status != "verified":
        found.append(gapmod.provisional_csv_schema(profile.name))
    if mapping.unmapped:
        found.append(gapmod.unmapped_csv_columns(mapping.unmapped))
    found.append(gapmod.bedrock_not_visible(detected=bedrock_seen))
    return found


def _cost_gaps(
    profile: Profile,
    mapping: Mapping_,
    frame: Frame,
    bedrock_seen: bool,
) -> list:
    """A cost export carries no segments, so it says nothing about grouping.

    Emitting DIMENSION_NOT_GROUPED from here would contradict the usage export
    sitting beside it -- the report would claim the data is not broken out by
    workspace while listing the workspaces.
    """
    found = _source_gaps(profile, mapping, bedrock_seen)
    cost_types = {r.cost_type for r in frame.cost}
    if "code_execution" in cost_types:
        found.append(gapmod.code_execution_cost_only())
    if "session_usage" in cost_types:
        found.append(gapmod.session_usage_cost_only())
    return found


def _usage_gaps(
    profile: Profile,
    mapping: Mapping_,
    frame: Frame,
    bedrock_seen: bool,
) -> list:
    found = _source_gaps(profile, mapping, bedrock_seen)

    # Token categories the export never carried. Each check that needs one
    # says so itself (Check.needs_tokens); this one says what the totals and
    # the estimate leave out. A TTL-summing source is covered by its own gap.
    absent = [f for f in TOKEN_FIELDS if not frame.has_token_field(f)]
    if frame.has_token_field(UNSPLIT_CACHE_WRITE):
        absent = [f for f in absent if f not in ("cache_write_5m", "cache_write_1h")]
    if absent:
        found.append(gapmod.token_columns_not_exported(tuple(absent)))

    # "Input tokens" may or may not include cached tokens; only a name that
    # says "uncached" settles it. Matters only beside separate cache columns.
    input_header = mapping.headers[mapping.fields["uncached_input"]]
    has_cache_columns = any(
        f in mapping.fields for f in ALL_TOKEN_FIELDS if f not in ("uncached_input", "output")
    )
    if "uncached" not in normalize(input_header) and has_cache_columns:
        found.append(gapmod.input_column_ambiguous(input_header))

    # Tiers we cannot price. Which *checks* a given export can support is the
    # check registry's business, not the parser's -- ingest naming a check
    # would drift the moment one was renamed, so those gaps are raised in
    # checks.run_checks() from each check's own declared preconditions.
    if "service_tier" in frame.grouped_by:
        for tier in frame.distinct("service_tier"):
            if tier and tier.startswith("priority"):
                found.append(gapmod.priority_tier_present(tier))

    return found
