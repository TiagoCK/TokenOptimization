"""CSV ingest for Console Usage and Cost page exports.

The path a prospect can actually use in a sales conversation: no admin key, no
security review, thirty seconds of work (PROJECT_BRIEF.md §3).

All header interpretation is delegated to a schema profile. This module decides
nothing about column names.
"""

from __future__ import annotations

import csv
from decimal import Decimal
from datetime import timedelta
from pathlib import Path

from .. import gaps as gapmod
from ..frame import (
    ALL_TOKEN_FIELDS,
    DIMENSIONS,
    TOKEN_FIELDS,
    CostRow,
    Frame,
    Segment,
    SourceInfo,
    TokenBuckets,
    UsageRow,
)
from .schema import (
    Mapping_,
    Profile,
    SchemaMismatch,
    infer_bucket_width,
    select_profile,
)

#: Markers that would positively indicate Bedrock-routed traffic. Absent one of
#: these there is no signal in aggregate data, so no claim is made -- see §2.
_BEDROCK_MARKERS = ("bedrock", "aws marketplace", "anthropic.claude-")

#: Fallback bucket length when an export has no end column and only one bucket.
_DEFAULT_BUCKET = timedelta(days=1)


def load_usage_csv(
    path: str | Path,
    profile: Profile | None = None,
) -> tuple[Frame, Mapping_]:
    """Read a Usage page export into a Frame."""
    if profile is None:
        # Auto-select: more than one producer writes these files, and they
        # disagree on column names and on cents-vs-dollars.
        p_probe = Path(path)
        headers_probe, _ = _read(p_probe)
        profile = select_profile(headers_probe, "usage")
    if profile.kind != "usage":
        raise SchemaMismatch(
            f"Profile {profile.name!r} describes {profile.kind!r} data, not usage.",
            headers=(),
            profile=profile.name,
            profile_path=profile.path,
        )

    p = Path(path)
    headers, records = _read(p)
    mapping = profile.match(headers)

    rows: list[UsageRow] = []
    bedrock_seen = False

    for row_no, record in enumerate(records, start=2):
        values = {
            name: profile.parse(profile.columns[name], _cell(record, idx), row_no=row_no)
            for name, idx in mapping.fields.items()
        }

        start = values.get("start")
        if start is None:
            continue  # blank trailing row
        end = values.get("end") or start + _DEFAULT_BUCKET

        rows.append(
            UsageRow(
                start=start,
                end=end,
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
    grouped = frozenset(d for d in DIMENSIONS if d in mapping.fields)

    frame = Frame(
        usage=tuple(rows),
        grouped_by=grouped,
        bucket_width=infer_bucket_width([r.start for r in rows]),
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
    return frame.with_gaps(_usage_gaps(profile, mapping, frame, bedrock_seen)), mapping


def load_cost_csv(
    path: str | Path,
    profile: Profile | None = None,
) -> tuple[Frame, Mapping_]:
    """Read a Cost page export into a Frame."""
    if profile is None:
        p_probe = Path(path)
        headers_probe, _ = _read(p_probe)
        profile = select_profile(headers_probe, "cost")
    if profile.kind != "cost":
        raise SchemaMismatch(
            f"Profile {profile.name!r} describes {profile.kind!r} data, not cost.",
            headers=(),
            profile=profile.name,
            profile_path=profile.path,
        )

    p = Path(path)
    headers, records = _read(p)
    mapping = profile.match(headers)

    # "123.45" in USD means $1.23 -- the API reports the lowest currency unit.
    # Converting once, here, is the only place this factor appears.
    divisor = Decimal("100") if profile.amount_units == "cents" else Decimal("1")

    rows: list[CostRow] = []
    bedrock_seen = False

    for row_no, record in enumerate(records, start=2):
        values = {
            name: profile.parse(profile.columns[name], _cell(record, idx), row_no=row_no)
            for name, idx in mapping.fields.items()
        }
        start = values.get("start")
        if start is None:
            continue
        end = values.get("end") or start + _DEFAULT_BUCKET

        rows.append(
            CostRow(
                start=start,
                end=end,
                amount_usd=(values.get("amount") or Decimal("0")) / divisor,
                currency=values.get("currency") or "USD",
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
    return frame.with_gaps(_cost_gaps(profile, mapping, frame, bedrock_seen)), mapping


# --------------------------------------------------------------------------
# internals
# --------------------------------------------------------------------------


def _read(path: Path) -> tuple[tuple[str, ...], list[list[str]]]:
    if not path.exists():
        raise FileNotFoundError(f"no such file: {path}")
    # utf-8-sig: Console exports opened in Excel routinely acquire a BOM, which
    # would otherwise corrupt the first header and fail the match.
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise SchemaMismatch(
            f"{path} is empty.", headers=(), profile="?", profile_path=None
        )
    return tuple(rows[0]), rows[1:]


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

    # Tiers we cannot price. Which *checks* a given export can support is the
    # check registry's business, not the parser's -- ingest naming a check
    # would drift the moment one was renamed, so those gaps are raised in
    # checks.run_checks() from each check's own declared preconditions.
    if "service_tier" in frame.grouped_by:
        for tier in frame.distinct("service_tier"):
            if tier and tier.startswith("priority"):
                found.append(gapmod.priority_tier_present(tier))

    return found
