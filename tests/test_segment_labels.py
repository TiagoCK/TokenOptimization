"""Evidence labels must not turn "not broken out" into "default workspace".

Invariant 3: a null inside a grouped dimension carries the API's meaning
(default workspace, Console playground), and an absent dimension means the
export never carried it. C1, C2 and C3 once labelled rows with
``segment.workspace_id or "default workspace"`` -- so an export with no
workspace column told the reader all of its spend was in the default
workspace, and one with no API key column called it Console traffic. Both are
claims the data never made.

Each check is exercised on an export that was never grouped by the dimension,
and on one where the null is genuine, and the two must read differently.
"""

from datetime import datetime, timezone

from claude_spend_audit.checks import CheckConfig
from claude_spend_audit.checks._format import describe
from claude_spend_audit.checks.c1_cache_amortization import CHECK as C1
from claude_spend_audit.checks.c2_cache_adoption import CHECK as C2
from claude_spend_audit.checks.c3_batch_tier import CHECK as C3
from claude_spend_audit.frame import Frame, Segment, TokenBuckets, UsageRow

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 2, tzinfo=timezone.utc)


def frame_of(segment: Segment, tokens: TokenBuckets, grouped: set[str]) -> Frame:
    return Frame(
        usage=(UsageRow(start=T0, end=T1, segment=segment, tokens=tokens),),
        grouped_by=frozenset(grouped),
        bucket_width="1d",
    )


#: A segment whose workspace and key are null -- genuinely, or because the
#: export never had them. Only grouped_by says which.
NULLS = Segment(model="claude-opus-5", service_tier="standard")

#: 1h writes never read back (C1), plenty of uncached input (C2), all on the
#: standard tier (C3). Enough to make each check speak.
TOKENS = TokenBuckets(
    uncached_input=10_000_000, cache_write_1h=2_000_000, output=1_000_000
)

UNGROUPED = {"model", "service_tier"}
GROUPED = {"model", "service_tier", "workspace_id", "api_key_id"}


def labels(check, grouped: set[str], pricing) -> list[str]:
    findings = list(check.run(frame_of(NULLS, TOKENS, grouped), pricing, CheckConfig()))
    assert len(findings) == 1
    return [row[0] for row in findings[0].evidence.rows]


# --------------------------------------------------------------------------
# The helper
# --------------------------------------------------------------------------


def test_absent_workspace_is_all_workspaces():
    frame = frame_of(NULLS, TOKENS, UNGROUPED)
    assert describe(frame, NULLS) == "claude-opus-5 / all workspaces"


def test_null_workspace_in_a_grouped_export_is_the_default_workspace():
    frame = frame_of(NULLS, TOKENS, GROUPED)
    assert describe(frame, NULLS) == "claude-opus-5 / default workspace"


def test_absent_key_is_never_called_console_traffic():
    frame = frame_of(NULLS, TOKENS, UNGROUPED)
    label = describe(frame, NULLS, ("model", "workspace_id", "api_key_id"))
    assert label == "claude-opus-5 / all workspaces / all keys"
    assert "Console" not in label


def test_null_key_in_a_grouped_export_is_console_traffic():
    frame = frame_of(NULLS, TOKENS, GROUPED)
    label = describe(frame, NULLS, ("model", "workspace_id", "api_key_id"))
    assert label == "claude-opus-5 / default workspace / no API key (Console)"


def test_named_values_pass_through():
    segment = Segment(model="claude-opus-5", workspace_id="wrkspc_a", api_key_id="apikey_b")
    frame = frame_of(segment, TOKENS, GROUPED)
    assert (
        describe(frame, segment, ("model", "workspace_id", "api_key_id"))
        == "claude-opus-5 / wrkspc_a / apikey_b"
    )


# --------------------------------------------------------------------------
# Each check that labels segments
# --------------------------------------------------------------------------


def test_c1_does_not_invent_default_workspace_or_console_traffic(pricing):
    assert labels(C1, UNGROUPED, pricing) == ["claude-opus-5 / all workspaces / all keys"]
    assert labels(C1, GROUPED, pricing) == [
        "claude-opus-5 / default workspace / no API key (Console)"
    ]


def test_c2_does_not_invent_a_default_workspace_or_console_traffic(pricing):
    assert labels(C2, UNGROUPED, pricing) == ["claude-opus-5 / all workspaces / all keys"]
    assert labels(C2, GROUPED, pricing) == [
        "claude-opus-5 / default workspace / no API key (Console)"
    ]


def test_c3_does_not_invent_a_default_workspace(pricing):
    assert labels(C3, UNGROUPED, pricing) == ["claude-opus-5 / all workspaces"]
    assert labels(C3, GROUPED, pricing) == ["claude-opus-5 / default workspace"]
