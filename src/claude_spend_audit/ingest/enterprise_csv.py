"""CSV ingest for the Team/Enterprise plan spend report.

The third ingest path, and the only one that does not produce a ``Frame``:
see ``enterprise.report`` for why. All header interpretation is delegated to
the ``enterprise_spend`` schema profile, exactly as the Console path delegates
to its own -- this module decides nothing about column names.

Which checks a given export can support is the enterprise check registry's
business, not the parser's, for the same reason as on the usage path: ingest
naming a check would drift the moment one was renamed.
"""

from __future__ import annotations

from pathlib import Path

from .. import gaps as gapmod
from ..enterprise.report import ALL_FIELDS, SpendReport, SpendRow
from ..frame import SourceInfo
from .csv_ingest import _cell, _read
from .schema import (
    DEFAULT_SCHEMA_DIR,
    Mapping_,
    Profile,
    SchemaError,
    SchemaMismatch,
    select_profile,
)

#: The schema profile ``kind`` for this export.
ENTERPRISE_KIND = "enterprise_spend"

#: A spend report needs at least one of these to be a spend report at all.
_NUMERIC = ("prompt_tokens", "completion_tokens", "net_spend_usd", "gross_spend_usd")


def load_enterprise_csv(
    path: str | Path,
    profile: Profile | None = None,
) -> tuple[SpendReport, Mapping_]:
    """Read a Team/Enterprise spend report into a SpendReport."""
    p = Path(path)
    headers, records = _read(p)
    if profile is None:
        profile = select_profile(headers, ENTERPRISE_KIND)
    if profile.kind != ENTERPRISE_KIND:
        raise SchemaMismatch(
            f"Profile {profile.name!r} describes {profile.kind!r} data, not a "
            "Team/Enterprise spend report.",
            headers=headers,
            profile=profile.name,
            profile_path=profile.path,
        )

    mapping = profile.match(headers)
    if not any(name in mapping.fields for name in _NUMERIC):
        # Emails and models with no numbers beside them is a member list, not
        # a spend report. Saying so beats reporting an account with no spend.
        raise SchemaMismatch(
            "This file matched the Team/Enterprise spend report profile on its "
            "user and model columns, but no token or spend column was "
            "recognised, so there is nothing to analyse.",
            headers=headers,
            profile=profile.name,
            profile_path=profile.path,
            missing=_NUMERIC,
        )

    rows: list[SpendRow] = []
    starts = []
    ends = []
    excluded = 0

    for row_no, record in enumerate(records, start=2):
        values = {
            name: profile.parse(profile.columns[name], _cell(record, idx), row_no=row_no)
            for name, idx in mapping.fields.items()
        }
        if values.get("period_start") is not None:
            starts.append(values["period_start"])
        if values.get("period_end") is not None:
            ends.append(values["period_end"])

        if values.get("user_email") is None and values.get("model") is None:
            excluded += 1
            continue

        rows.append(
            SpendRow(**{name: values.get(name) for name in ALL_FIELDS if name in mapping.fields})
        )

    report = SpendReport(
        rows=tuple(rows),
        present=frozenset(name for name in ALL_FIELDS if name in mapping.fields),
        period_start=min(starts) if starts else None,
        period_end=max(ends) if ends else None,
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

    found = []
    if profile.status != "verified":
        found.append(
            gapmod.provisional_csv_schema(
                profile.name,
                "every column header of the Team/Enterprise spend report -- "
                "only some are named in its documentation",
            )
        )
    if mapping.unmapped:
        found.append(gapmod.unmapped_csv_columns(mapping.unmapped))
    if excluded:
        found.append(gapmod.enterprise_rows_excluded(excluded))
    found.append(gapmod.enterprise_analytics_source())
    return report.with_gaps(found), mapping


def detect_enterprise(path: str | Path | None) -> Profile | None:
    """The enterprise profile this file matches, or None.

    Only claims a file that no usage profile matches, so routing on it can
    never take a Console or cookbook export away from the usage path. A file
    that cannot be read as CSV at all is simply not detected; the usage path
    then reports its own error for it.
    """
    if path is None:
        return None
    try:
        headers, _ = _read(Path(path))
    except (OSError, UnicodeDecodeError, SchemaMismatch):
        return None
    try:
        select_profile(headers, "usage")
        return None
    except (SchemaMismatch, SchemaError):
        pass
    try:
        return select_profile(headers, ENTERPRISE_KIND)
    except (SchemaMismatch, SchemaError):
        pass
    # A file that has every required column but claims one field twice is a
    # spend report with a problem, not usage data. Route it here so the user
    # sees the ambiguity error that names the two columns, rather than a
    # usage-profile mismatch that says nothing useful about this file.
    for candidate in sorted(DEFAULT_SCHEMA_DIR.glob("*.json")):
        try:
            profile = Profile.load(candidate)
        except SchemaError:
            continue
        if profile.kind != ENTERPRISE_KIND:
            continue
        try:
            profile.match(headers)
        except SchemaMismatch as exc:
            if not exc.missing:
                return profile
        except SchemaError:
            continue
    return None
