"""The check registry: preconditions, and §7's "never touch the reporter" rule."""

from datetime import datetime, timezone
from decimal import Decimal

from claude_spend_audit.checks import CHECKS, Check, CheckConfig, run_checks
from claude_spend_audit.findings import Confidence, Finding
from claude_spend_audit.frame import Frame, Segment, TokenBuckets, UsageRow
from claude_spend_audit.gaps import GapCode
from claude_spend_audit.ingest import load_usage_csv
from claude_spend_audit.render import terminal

T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 2, tzinfo=timezone.utc)


def a_frame(**kwargs) -> Frame:
    defaults = dict(
        usage=(
            UsageRow(
                start=T0,
                end=T1,
                segment=Segment(model="claude-opus-5"),
                tokens=TokenBuckets(uncached_input=1_000, output=100),
            ),
        ),
        grouped_by=frozenset({"model"}),
        bucket_width="1d",
    )
    return Frame(**(defaults | kwargs))


# --------------------------------------------------------------------------
# Preconditions
# --------------------------------------------------------------------------


def test_missing_dimension_produces_a_gap_naming_the_check():
    check = Check(
        id="CX",
        name="Needs a workspace",
        run=lambda f, p, c: [],
        needs_dimensions=frozenset({"workspace_id"}),
    )
    unmet = check.unmet(a_frame())
    assert len(unmet) == 1
    assert unmet[0].code is GapCode.DIMENSION_NOT_GROUPED
    assert "Needs a workspace (CX)" in unmet[0].message
    # And it says this is about the export, not about the spend.
    assert "property of the export, not a finding" in unmet[0].message


def test_wrong_granularity_produces_a_gap_naming_the_check():
    check = Check(
        id="CY",
        name="Needs hours",
        run=lambda f, p, c: [],
        needs_bucket_width=frozenset({"1h"}),
        prefers_bucket_width="1h",
    )
    unmet = check.unmet(a_frame(bucket_width="1d"))
    assert len(unmet) == 1
    assert unmet[0].code is GapCode.GRANULARITY_TOO_COARSE
    assert "Needs hours (CY)" in unmet[0].message


def test_indeterminate_granularity_counts_as_unmet():
    """One bucket gives no spacing to infer from. Guessing would let an
    off-hours check run on daily data."""
    check = Check(
        id="CY", name="Needs hours", run=lambda f, p, c: [], needs_bucket_width=frozenset({"1h"})
    )
    assert check.unmet(a_frame(bucket_width=None))


def test_a_dimension_that_is_null_everywhere_does_not_count_as_grouped():
    """Frame.grouped_by is the authority, not the presence of values.

    This is the M1 invariant that stops C7 reporting un-grouped data as
    unattributed spend; checks inherit it for free.
    """
    check = Check(
        id="CX",
        name="Needs a workspace",
        run=lambda f, p, c: [],
        needs_dimensions=frozenset({"workspace_id"}),
    )
    frame = a_frame(grouped_by=frozenset({"model"}))
    assert frame.usage[0].segment.workspace_id is None
    assert check.unmet(frame)


def test_a_blocked_check_does_not_run(pricing):
    ran = []

    def spy(frame, pricing, config):
        ran.append(True)
        return []

    check = Check(
        id="CZ", name="Blocked", run=spy, needs_dimensions=frozenset({"api_key_id"})
    )
    run = run_checks(a_frame(), pricing, CheckConfig(), checks=[check])
    assert ran == []
    assert [label for label, _ in run.skipped] == ["Blocked (CZ)"]


def test_clean_checks_are_distinguished_from_blocked_ones(pricing):
    """"Ran and found nothing" and "could not run" are different claims and
    the report must not conflate them."""
    clean = Check(id="OK", name="Clean", run=lambda f, p, c: [])
    blocked = Check(
        id="NO",
        name="Blocked",
        run=lambda f, p, c: [],
        needs_bucket_width=frozenset({"1m"}),
    )
    run = run_checks(a_frame(), pricing, CheckConfig(), checks=[clean, blocked])
    assert run.clean == ("Clean (OK)",)
    assert [label for label, _ in run.skipped] == ["Blocked (NO)"]


# --------------------------------------------------------------------------
# §7: adding a check must never require touching the reporter
# --------------------------------------------------------------------------


def test_the_renderer_handles_a_check_it_has_never_heard_of(pricing):
    """The reporter iterates findings; it knows nothing about check identity."""
    invented = Finding(
        check_id="C99",
        title="An entirely new kind of finding",
        confidence=Confidence.OBSERVATION,
        summary="Something the renderer has no special case for.",
        measured_usd=Decimal("12.34"),
        measured_basis="an invented basis",
    )
    check = Check(id="C99", name="Invented", run=lambda f, p, c: [invented])
    run = run_checks(a_frame(), pricing, CheckConfig(), checks=[check])

    rendered = "\n".join(terminal.render_findings(run.findings))
    assert "C99" in rendered
    assert "$12.34" in rendered
    assert "an invented basis" in rendered
    assert "observation" in rendered


def test_findings_come_back_ranked(pricing):
    measured = Finding(
        check_id="M",
        title="m",
        confidence=Confidence.MEASURED,
        summary="s",
        measured_usd=Decimal("1"),
        measured_basis="b",
    )
    observation = Finding(
        check_id="O",
        title="o",
        confidence=Confidence.OBSERVATION,
        summary="s",
        measured_usd=Decimal("9999"),
        measured_basis="b",
    )
    checks = [
        Check(id="O", name="obs", run=lambda f, p, c: [observation]),
        Check(id="M", name="meas", run=lambda f, p, c: [measured]),
    ]
    run = run_checks(a_frame(), pricing, CheckConfig(), checks=checks)
    assert [f.check_id for f in run.findings] == ["M", "O"]


# --------------------------------------------------------------------------
# Ingest no longer names checks
# --------------------------------------------------------------------------


def test_ingest_does_not_hardcode_check_names(usage_profile):
    """Ingest emits facts about the data; the registry owns check identity.

    Before M2 the CSV loader raised "batch tier adoption (C3)" from inside the
    parser -- a string that would go stale the moment a check was renamed, for
    a check that did not exist yet.
    """
    frame, _ = load_usage_csv("tests/fixtures/csv/usage_console.csv", usage_profile)
    messages = " ".join(g.message for g in frame.gaps)
    assert "C3" not in messages
    assert "C6" not in messages
    assert "C7" not in messages


def test_precondition_gaps_come_from_the_registry(pricing, usage_profile):
    """The same information, now sourced from the checks that actually exist."""
    frame, _ = load_usage_csv("tests/fixtures/csv/usage_console.csv", usage_profile)
    run = run_checks(frame, pricing, CheckConfig())
    messages = " ".join(g.message for g in run.gaps)
    assert "C6b" in messages


def test_registered_check_ids_are_unique():
    ids = [c.id for c in CHECKS]
    assert len(ids) == len(set(ids))


def test_every_registered_check_declares_a_name():
    for check in CHECKS:
        assert check.name
        assert check.label == f"{check.name} ({check.id})"
