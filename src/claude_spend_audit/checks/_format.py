"""Display helpers shared by the checks' evidence tables.

Formatting only -- nothing here computes a figure a finding relies on. Kept
in one place because three checks had grown their own copies.
"""

from __future__ import annotations

from decimal import Decimal

from ..frame import Segment

_MILLION = Decimal("1000000")
_THOUSAND = Decimal("1000")


def short_tokens(count: int) -> str:
    """Compact token counts so an evidence table fits a terminal width."""
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


def model_workspace(segment: Segment) -> str:
    """The model / workspace label C2, C3 and C5 roll segments up under."""
    model = segment.model or "unknown model"
    workspace = segment.workspace_id or "default workspace"
    return f"{model} / {workspace}"
