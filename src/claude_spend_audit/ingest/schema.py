"""Header-to-field matching for CSV exports, driven by JSON profiles.

Anthropic does not publish the Console export's column headers. PROJECT_BRIEF.md
§7 says never to invent an API field, so the mapping lives in a JSON file the
user can inspect and correct -- the same treatment §3 requires for pricing --
rather than in a table of guesses compiled into the parser.

The three rules that make this honest rather than merely flexible:

* an ambiguous match is an error, never silently resolved
* an unmapped column is reported, never silently dropped
* a missing required column raises with the real header row attached
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping

DEFAULT_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schemas"


class SchemaError(Exception):
    """The profile itself is unusable."""


class SchemaMismatch(Exception):
    """The file's headers do not match the profile.

    Carries the actual header row so the user can fix the profile in one pass
    instead of guessing at what the parser wanted.
    """

    def __init__(
        self,
        message: str,
        *,
        headers: tuple[str, ...],
        profile: str,
        profile_path: Path | None = None,
        missing: tuple[str, ...] = (),
    ):
        self.headers = headers
        self.profile = profile
        self.profile_path = profile_path
        self.missing = missing

        lines = [message, ""]
        lines.append(f"  profile:  {profile}")
        if profile_path:
            lines.append(f"  file:     {profile_path}")
        if missing:
            lines.append(f"  missing:  {', '.join(missing)}")
        lines.append("")
        lines.append("  columns actually present in the export:")
        for header in headers:
            lines.append(f"    - {header!r}")
        lines.append("")
        lines.append(
            "  These headers are not published by Anthropic, so this profile is a "
            "best guess."
        )
        lines.append(
            "  Add the correct spelling to the relevant field's 'aliases' in the "
            "profile"
        )
        lines.append("  and re-run. No code change is needed.")
        super().__init__("\n".join(lines))


#: Zero-width and BOM characters that make two identical-looking identifiers
#: different strings. The cookbook's own sample output lists the same model
#: twice, which is what this defends against.
#:
#: Only genuinely *zero-width* characters belong here, because they are
#: deleted. A non-breaking space (U+00A0) is a space, not an invisible: it is
#: handled by the split/join below, which treats every Unicode space alike.
#: Deleting it would turn "Claude<nbsp>Opus<nbsp>5" into "ClaudeOpus5".
_INVISIBLE = dict.fromkeys([0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF], None)


def normalize_value(raw: str) -> str:
    """Clean a dimension value so two spellings of one thing compare equal.

    Trims, collapses internal whitespace, and strips zero-width characters.
    Deliberately conservative: it does NOT change case or punctuation, because
    "Claude Opus 5" and "claude-opus-5" are different strings a user chose and
    silently merging them would be rewriting their data. Genuine aliases are
    *detected* instead -- see PricingTable.alias_collisions.
    """
    cleaned = raw.translate(_INVISIBLE)
    cleaned = "".join(ch for ch in cleaned if ch.isprintable() or ch.isspace())
    return " ".join(cleaned.split())


def normalize(header: str) -> str:
    """Fold a header for matching: case, whitespace, and separators."""
    folded = header.strip().lower()
    for ch in ("_", "-", "/", "."):
        folded = folded.replace(ch, " ")
    return " ".join(folded.split())


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    name: str
    type: str
    required: bool = False
    dimension: bool = False
    aliases: tuple[str, ...] = ()
    token_type: str | None = None


@dataclass(slots=True)
class Mapping_:
    """The result of matching one file's headers against a profile."""

    #: canonical field name -> column index in the CSV
    fields: dict[str, int] = field(default_factory=dict)
    #: headers that matched nothing
    unmapped: tuple[str, ...] = ()
    #: the header row as it appeared
    headers: tuple[str, ...] = ()

    def index_of(self, name: str) -> int | None:
        return self.fields.get(name)

    @property
    def dimensions(self) -> frozenset[str]:
        return frozenset(self.fields)

    def explain(self, profile: Profile) -> str:
        """Human-readable mapping table for --explain-schema."""
        lines = [
            f"schema profile: {profile.name}  [{profile.status}]",
        ]
        if profile.path:
            lines.append(f"profile file:   {profile.path}")
        if profile.status == "provisional":
            lines.append(
                "                (unverified -- Anthropic does not publish "
                "these headers)"
            )
        lines.append("")
        lines.append(f"{'CSV column':<34}  ->  canonical field")
        lines.append(f"{'-' * 34}      {'-' * 24}")
        by_index = {i: n for n, i in self.fields.items()}
        for i, header in enumerate(self.headers):
            target = by_index.get(i)
            arrow = f"->  {target}" if target else "->  (ignored)"
            lines.append(f"{header[:34]:<34}  {arrow}")

        missing_optional = [
            c.name
            for c in profile.columns.values()
            if c.name not in self.fields and not c.required
        ]
        if missing_optional:
            lines.append("")
            lines.append("fields not present in this export:")
            for name in missing_optional:
                lines.append(f"  - {name}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    kind: str  # "usage" | "cost"
    status: str  # "provisional" | "verified"
    columns: Mapping[str, ColumnSpec]
    null_tokens: frozenset[str]
    amount_units: str = "cents"
    path: Path | None = None
    verified_against: str | None = None

    # -- loading ---------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> Profile:
        p = Path(path)
        if not p.exists():
            raise SchemaError(f"schema profile not found: {p}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SchemaError(f"{p}: invalid JSON: {exc}") from exc
        return cls.from_dict(data, path=p)

    @classmethod
    def load_named(cls, name: str, directory: str | Path | None = None) -> Profile:
        d = Path(directory) if directory else DEFAULT_SCHEMA_DIR
        path = d / f"{name}.json"
        if not path.exists():
            # Profiles live at the repo root for the same reason rates do: they
            # are meant to be edited when an export does not match.
            raise SchemaError(
                f"schema profile not found: {path}\n"
                "  Profiles are kept in a top-level schemas/ directory so you "
                "can correct them.\n"
                "  Run from a checkout (pip install -e .), or pass "
                "--usage-profile / --cost-profile."
            )
        return cls.load(path)

    @classmethod
    def from_dict(cls, data: Mapping[str, object], *, path: Path | None = None) -> Profile:
        raw_columns = data.get("columns")
        if not isinstance(raw_columns, dict) or not raw_columns:
            raise SchemaError(f"{path or '<memory>'}: profile has no columns")

        columns: dict[str, ColumnSpec] = {}
        for name, spec in raw_columns.items():
            if not isinstance(spec, dict):
                raise SchemaError(f"{path}: column {name!r} is not an object")
            columns[name] = ColumnSpec(
                name=name,
                type=str(spec.get("type", "string")),
                required=bool(spec.get("required", False)),
                dimension=bool(spec.get("dimension", False)),
                aliases=tuple(str(a) for a in spec.get("aliases", ())),
                token_type=spec.get("token_type"),
            )

        return cls(
            name=str(data.get("profile") or (path.stem if path else "unnamed")),
            kind=str(data.get("kind") or "usage"),
            status=str(data.get("status") or "provisional"),
            columns=columns,
            null_tokens=frozenset(
                normalize(str(t)) for t in data.get("null_tokens", ("",))
            ),
            amount_units=str(data.get("amount_units") or "cents"),
            path=path,
            verified_against=data.get("verified_against"),  # type: ignore[arg-type]
        )

    # -- matching --------------------------------------------------------

    def match(self, headers: tuple[str, ...]) -> Mapping_:
        """Match a header row, or raise SchemaMismatch with the row attached."""
        # alias -> canonical field. Building this per-match keeps the collision
        # check honest even if a profile is edited between runs.
        alias_index: dict[str, str] = {}
        for spec in self.columns.values():
            for alias in (spec.name, *spec.aliases):
                key = normalize(alias)
                existing = alias_index.get(key)
                if existing and existing != spec.name:
                    raise SchemaError(
                        f"{self.path or self.name}: alias {alias!r} is claimed by "
                        f"both {existing!r} and {spec.name!r}"
                    )
                alias_index[key] = spec.name

        fields: dict[str, int] = {}
        claimed_by: dict[str, str] = {}
        unmapped: list[str] = []

        for i, header in enumerate(headers):
            target = alias_index.get(normalize(header))
            if target is None:
                unmapped.append(header)
                continue
            if target in fields:
                raise SchemaMismatch(
                    f"Ambiguous export: columns {claimed_by[target]!r} and "
                    f"{header!r} both map to field {target!r}. Refusing to "
                    f"guess which one carries the data.",
                    headers=headers,
                    profile=self.name,
                    profile_path=self.path,
                )
            fields[target] = i
            claimed_by[target] = header

        missing = tuple(
            spec.name
            for spec in self.columns.values()
            if spec.required and spec.name not in fields
        )
        if missing:
            raise SchemaMismatch(
                f"This export does not match schema profile {self.name!r}: "
                f"{len(missing)} required column(s) could not be matched.",
                headers=headers,
                profile=self.name,
                profile_path=self.path,
                missing=missing,
            )

        return Mapping_(fields=fields, unmapped=tuple(unmapped), headers=headers)

    # -- value parsing ---------------------------------------------------

    def is_null(self, raw: str) -> bool:
        return normalize(raw) in self.null_tokens

    def parse(self, spec: ColumnSpec, raw: str, *, row_no: int):
        """Parse one cell according to its declared type."""
        if self.is_null(raw):
            if spec.type == "int":
                return 0
            if spec.type == "decimal":
                return Decimal("0")
            return None

        text = raw.strip()
        try:
            if spec.type == "int":
                # Exports often carry thousands separators.
                return int(Decimal(text.replace(",", "")))
            if spec.type == "decimal":
                return Decimal(text.replace(",", "").replace("$", ""))
            if spec.type == "datetime":
                return parse_datetime(text)
            return normalize_value(text)
        except (ValueError, InvalidOperation, ArithmeticError) as exc:
            raise SchemaMismatch(
                f"Row {row_no}: could not read {raw!r} as {spec.type} for field "
                f"{spec.name!r}.",
                headers=(),
                profile=self.name,
                profile_path=self.path,
            ) from exc


_DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
)


def parse_datetime(text: str) -> datetime:
    """Parse a timestamp to a tz-aware UTC datetime.

    Naive timestamps are read as UTC: the API snaps every bucket to UTC
    boundaries, and silently treating them as local time would shift every
    bucket and corrupt C6's off-hours detection.
    """
    text = text.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in _DATETIME_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise ValueError(f"unrecognised timestamp {text!r}")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def select_profile(
    headers: tuple[str, ...],
    kind: str,
    directory: str | Path | None = None,
) -> Profile:
    """Find the profile whose columns this header row satisfies.

    There is more than one producer of these CSVs -- the Console UI export and
    Anthropic's cookbook exporter -- and they disagree on column names and, in
    the cost file, on whether amounts are cents or dollars. Guessing wrong on
    the latter is a 100x error, so the file is matched against every known
    profile rather than assumed.

    Selection is by *coverage*, not by first match. Several profiles can
    satisfy the same header row -- the cookbook profile's required columns are
    a subset of the Console export's, so "first verified profile that matches"
    would claim every Console file. The profile that recognises the most of the
    file is the one that actually describes it: a Console export carries
    columns (`context_window`, `inference_geo`, the split cache TTLs) that the
    cookbook profile has never heard of, and vice versa for
    `cache_creation_tokens`.

    Ties break toward the verified profile, then by name so the choice is
    deterministic.
    """
    d = Path(directory) if directory else DEFAULT_SCHEMA_DIR
    candidates = [
        Profile.load(path)
        for path in sorted(d.glob("*.json"))
    ]
    candidates = [p for p in candidates if p.kind == kind]
    if not candidates:
        raise SchemaError(f"no {kind!r} schema profiles found in {d}")

    matched: list[tuple[int, bool, str, Profile]] = []
    attempts: list[str] = []
    for profile in candidates:
        try:
            mapping = profile.match(headers)
        except SchemaMismatch as exc:
            missing = ", ".join(exc.missing) if exc.missing else "ambiguous match"
            attempts.append(f"    {profile.name} [{profile.status}]: {missing}")
            continue
        matched.append(
            (len(mapping.unmapped), profile.status != "verified", profile.name, profile)
        )

    if matched:
        matched.sort(key=lambda item: item[:3])
        return matched[0][3]

    detail = "\n".join(attempts)
    raise SchemaMismatch(
        f"No {kind} schema profile matches this file. Tried "
        f"{len(candidates)}, each failing on:\n{detail}",
        headers=headers,
        profile=", ".join(p.name for p in candidates),
        profile_path=d,
    )


def infer_bucket_width(starts: list[datetime]) -> str | None:
    """Infer 1m / 1h / 1d from the spacing of distinct bucket starts.

    Returns None rather than guessing when the spacing is irregular or there is
    only one bucket -- C6 needs to know it has hourly data, and a wrong answer
    there would let an off-hours check run on daily buckets.
    """
    distinct = sorted(set(starts))
    if len(distinct) < 2:
        return None
    deltas = {
        int((b - a).total_seconds()) for a, b in zip(distinct, distinct[1:])
    }
    smallest = min(deltas)
    return {60: "1m", 3600: "1h", 86400: "1d"}.get(smallest)
