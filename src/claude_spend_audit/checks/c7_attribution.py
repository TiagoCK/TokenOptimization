"""C7 -- attribution coverage. `observation`

PROJECT_BRIEF.md §4: "High unattributed spend is itself the finding -- you
cannot manage what you cannot allocate, and this is often the most persuasive
slide in the readout."

This is the check the whole `Frame.grouped_by` design exists to protect. The
API returns `null` for `workspace_id` both when the traffic belongs to the
default workspace *and* when the caller did not group by workspace. Conflating
those two would let an export that simply was not broken out report as 100%
unattributed spend -- a fabricated finding, on the check where a fabrication
would be most damaging.

So C7 declares `needs_dimensions`. An un-grouped export produces a gap naming
this check, never a number.

**The two causes are reported separately**, because they are different problems
and conflating them invites a fair objection. Spend with no workspace is
default-workspace traffic: real, attributable to a team in principle, but not
allocatable from this data. Spend with no API key is Console playground
traffic: a person clicking in a browser, which usually *should not* be a
material line in a production bill. An engineer whose main workspace is the
default one will reject a finding that lumps those together, and they would be
right to.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from .. import gaps as gapmod
from ..frame import Frame
from ..findings import Confidence, Evidence, Finding
from ..pricing import PricingTable


def run(frame: Frame, pricing: PricingTable, config) -> Iterable[Finding]:
    total = Decimal("0")
    no_workspace = Decimal("0")
    no_key = Decimal("0")
    neither = Decimal("0")

    has_workspace_dim = frame.has_dimension("workspace_id")
    has_key_dim = frame.has_dimension("api_key_id")

    for row in frame.usage:
        estimate = pricing.estimate_tokens(row.tokens, row.segment)
        if not estimate.priced_tokens:
            continue
        total += estimate.usd

        # Only read a dimension the source actually carried. `value_of` returns
        # the NOT_GROUPED sentinel otherwise, which is not the same as null.
        missing_workspace = (
            has_workspace_dim and row.segment.workspace_id is None
        )
        missing_key = has_key_dim and row.segment.api_key_id is None

        if missing_workspace and missing_key:
            neither += estimate.usd
        elif missing_workspace:
            no_workspace += estimate.usd
        elif missing_key:
            no_key += estimate.usd

    if total <= 0:
        return []

    unattributed = no_workspace + no_key + neither
    share = unattributed / total
    if share < config.attribution_min_share or unattributed < config.min_impact_usd:
        return []

    # Mutually exclusive buckets, so the column sums to the total. Overlapping
    # ones ("no workspace" and "no key" each including the both-null spend)
    # add up to more than 100% and read as double counting.
    buckets = [
        ("default workspace, and no API key", neither),
        ("default workspace (named key)", no_workspace),
        ("Console playground (no API key)", no_key),
        ("attributable to a named workspace and key", total - unattributed),
    ]
    rows: list[tuple[str, str, str]] = [
        (label, f"${amount:,.2f}", f"{amount / total:.0%}")
        for label, amount in buckets
        if amount > 0
    ]

    dimensions = [
        d for d, present in (("workspace_id", has_workspace_dim), ("api_key_id", has_key_dim))
        if present
    ]

    return [
        Finding(
            check_id="C7",
            title="Spend that cannot be allocated to a named workspace or key",
            confidence=Confidence.OBSERVATION,
            scope=f"{share:.0%} of priced spend",
            summary=(
                f"{share:.0%} of priced spend (${unattributed:,.2f}) carries no "
                f"named workspace or API key. That is not waste, and this check "
                f"claims no savings -- but it is spend no one can be asked to "
                f"account for."
            ),
            measured_usd=unattributed,
            measured_basis=(
                "priced spend whose workspace or API key is null in the source "
                "data"
            ),
            # An observation makes no savings claim, by §3's definition.
            evidence=Evidence(
                columns=("attribution", "spend", "share"),
                rows=tuple(rows),
                note=(
                    "A null workspace means the default workspace; a null API "
                    "key means Console playground traffic. Both are normal, and "
                    "neither is an error -- they are simply not allocatable from "
                    "this data. Grouped by: " + ", ".join(dimensions) + "."
                ),
            ),
            remedy=(
                "Move production traffic into named workspaces with per-team API "
                "keys. Playground spend that is material is worth a second look "
                "on its own: it means someone is doing production-scale work in "
                "a browser."
            ),
            caveats=(
                "Default-workspace spend is attributable in principle -- someone "
                "knows whose it is. It is this data source that cannot say.",
                "This is a measure of how the account is organised, not of "
                "whether the money was well spent.",
            ),
        )
    ]


from . import Check  # noqa: E402

CHECK = Check(
    id="C7",
    name="Attribution coverage",
    run=run,
    # The load-bearing declaration. Without it, an export that was never
    # broken out by workspace would read as 100% unattributed.
    needs_dimensions=frozenset({"workspace_id"}),
)
