"""Display helpers shared by the checks' evidence tables.

Formatting only -- nothing here computes a figure a finding relies on. Kept
in one place because three checks had grown their own copies.
"""

from __future__ import annotations

from decimal import Decimal

from ..frame import NOT_GROUPED, Frame, Segment

_BILLION = Decimal("1000000000")
_MILLION = Decimal("1000000")
_THOUSAND = Decimal("1000")

#: What a null means *inside* a grouped dimension -- the API's own meaning.
_NULL_MEANS = {
    "model": "unknown model",
    "workspace_id": "default workspace",
    "api_key_id": "no API key (Console)",
}
#: What an absent dimension means: the export never broke it out, so the row
#: covers every value at once. Saying "default workspace" here would be the
#: exact conflation Frame.value_of exists to prevent.
_ABSENT_MEANS = {
    "model": "all models",
    "workspace_id": "all workspaces",
    "api_key_id": "all keys",
}


def short_tokens(count: int) -> str:
    """Compact token counts so an evidence table fits a terminal width."""
    if count >= 1_000_000_000:
        return f"{Decimal(count) / _BILLION:.2f}B"
    if count >= 1_000_000:
        return f"{Decimal(count) / _MILLION:.1f}M"
    if count >= 1_000:
        return f"{Decimal(count) / _THOUSAND:.0f}k"
    return str(count)


def multiple(value: Decimal) -> str:
    """A rate relative to another, e.g. ``0.1x`` or ``0.025x``.

    Three decimal places is enough to show Fable 5.1's 0.025x cache reads
    exactly, and rounds a blended 0.0999x to the 0.1x it effectively is.
    """
    return f"{value.quantize(Decimal('0.001')).normalize():f}x"


def describe(
    frame: Frame,
    segment: Segment,
    dimensions: tuple[str, ...] = ("model", "workspace_id"),
) -> str:
    """A segment's label, e.g. ``claude-opus-5 / wrkspc_x``.

    Reads each dimension through ``Frame.value_of``, so a null in a grouped
    dimension and a dimension the export never carried get different words.
    An export with no workspace column says "all workspaces", never "default
    workspace"; one with no API key column never claims Console traffic.
    """
    parts = []
    for dimension in dimensions:
        value = frame.value_of(segment, dimension)
        if value == NOT_GROUPED:
            parts.append(_ABSENT_MEANS[dimension])
        elif value is None:
            parts.append(_NULL_MEANS[dimension])
        else:
            parts.append(value)
    return " / ".join(parts)
