"""C6 -- spikes and off-hours volume, with the figures hand-calculated.

tests/fixtures/csv/usage_hourly.csv is 12 days of hourly buckets on
claude-sonnet-5 (input $2/MTok, output $10/MTok), starting Monday 2026-08-03.

A normal weekday:

    business hours   9h x (200,000 in + 40,000 out)
                     9 x ($0.40 + $0.40)                  = $7.20
    overnight 00-06  6h x (150,000 in + 30,000 out)
                     6 x ($0.30 + $0.30)                  = $3.60
    quiet, the rest  9h x (20,000 in + 4,000 out)
                     9 x ($0.04 + $0.04)                  = $0.72
                                                            -----
                                                            $11.52

A weekend day has no business hours: $3.60 + 18h x $0.08 = $5.04.

2026-08-11 (a Tuesday) is multiplied by 6: $69.12.
"""

from datetime import datetime, timezone
from decimal import Decimal

from claude_spend_audit.checks import CheckConfig, run_checks
from claude_spend_audit.checks.c6_anomalies import OFF_HOURS, SPIKES
from claude_spend_audit.findings import Confidence
from claude_spend_audit.gaps import GapCode
from claude_spend_audit.ingest import load_usage_csv

HOURLY = "tests/fixtures/csv/usage_hourly.csv"


def only(findings):
    assert len(findings) == 1, f"expected one finding, got {len(findings)}"
    return findings[0]


def hourly_frame(usage_profile):
    frame, _ = load_usage_csv(HOURLY, usage_profile)
    return frame


# --------------------------------------------------------------------------
# C6a -- spikes
# --------------------------------------------------------------------------


def test_spike_is_found(pricing, usage_profile):
    """2026-08-11 at $69.12 against an $11.52 median baseline.

        multiple = 69.12 / 11.52 = 6.0x
        excess   = 69.12 - 11.52 = $57.60
    """
    finding = only(list(SPIKES.run(hourly_frame(usage_profile), pricing, CheckConfig())))
    assert finding.check_id == "C6a"
    assert finding.measured_usd == Decimal("57.60")
    assert "2026-08-11" in finding.summary


def test_spike_claims_no_savings(pricing, usage_profile):
    """The magnitude is arithmetic; recoverability is not.

    Aggregate data cannot tell a stuck agent from a product launch, so C6
    measures and stops. §4 on C8: overclaiming is the fastest way to lose a
    technical buyer.
    """
    finding = only(list(SPIKES.run(hourly_frame(usage_profile), pricing, CheckConfig())))
    assert finding.confidence is Confidence.MEASURED
    assert finding.recoverable is None
    assert any("claimed as recoverable" in c for c in finding.caveats)
    assert any("measurement, not a verdict" in c for c in finding.caveats)


def test_spike_baseline_uses_a_median_not_a_mean(pricing, usage_profile):
    """A mean would be dragged upward by the spike sitting inside the window,
    shrinking the very multiple it is meant to detect."""
    finding = only(list(SPIKES.run(hourly_frame(usage_profile), pricing, CheckConfig())))
    row = next(r for r in finding.evidence.rows if r[0] == "2026-08-11")
    assert row[2] == "$11.52"  # the median weekday, not a spike-inflated mean
    assert row[3] == "6.0x"


def test_a_higher_threshold_silences_it(pricing, usage_profile):
    """A 6x day does not clear a 10x bar."""
    config = CheckConfig(spike_threshold=Decimal("10"))
    assert list(SPIKES.run(hourly_frame(usage_profile), pricing, config)) == []


def test_absolute_floor_suppresses_trivial_multiples(pricing, usage_profile):
    """The multiple alone would flag cents rising to dollars.

    Raising the floor above the $57.60 excess drops the finding even though
    the 6x multiple is unchanged.
    """
    config = CheckConfig(min_impact_usd=Decimal("100"))
    assert list(SPIKES.run(hourly_frame(usage_profile), pricing, config)) == []


def test_spike_runs_on_daily_data(pricing, usage_profile):
    """C6a needs days, not hours -- so a 1d export is enough.

    This is why C6 is registered as two checks: lumping them would lose spike
    detection on every daily export.
    """
    assert SPIKES.needs_bucket_width == frozenset({"1d", "1h"})
    frame, _ = load_usage_csv("tests/fixtures/csv/usage_console.csv", usage_profile)
    assert frame.bucket_width == "1d"
    assert SPIKES.unmet(frame) == []


def test_too_little_history_reports_nothing(pricing, usage_profile):
    """With no trustworthy baseline, silence beats a fabricated multiple."""
    config = CheckConfig(spike_min_history_days=30)
    assert list(SPIKES.run(hourly_frame(usage_profile), pricing, config)) == []


# --------------------------------------------------------------------------
# C6b -- off-hours
# --------------------------------------------------------------------------


def test_off_hours_share(pricing, usage_profile):
    """Business hours vs everything else, at the UTC default.

        weekdays in window   10 x $7.20                    = $72.00
        spike day adds       5 x $7.20 (the 6x multiplier)  = $36.00
        business total                                       $108.00

        overnight + quiet + weekends                          $74.88
        share = 74.88 / 182.88                               = 41%
    """
    finding = only(
        list(OFF_HOURS.run(hourly_frame(usage_profile), pricing, CheckConfig()))
    )
    assert finding.check_id == "C6b"
    assert finding.measured_usd == Decimal("74.88")
    assert "41%" in finding.summary


def test_off_hours_splits_overnight_from_weekend(pricing, usage_profile):
    """Weekend days are 2026-08-08 and 09: 2 x $5.04 = $10.08."""
    finding = only(
        list(OFF_HOURS.run(hourly_frame(usage_profile), pricing, CheckConfig()))
    )
    assert "$10.08" in finding.summary  # weekends
    assert "$64.80" in finding.summary  # overnight on weekdays


def test_off_hours_claims_no_savings(pricing, usage_profile):
    finding = only(
        list(OFF_HOURS.run(hourly_frame(usage_profile), pricing, CheckConfig()))
    )
    assert finding.confidence is Confidence.MEASURED
    assert finding.recoverable is None


def test_the_timezone_assumption_is_stated_not_buried(pricing, usage_profile):
    """The figure moves with the assumed window, so the window is on the page."""
    finding = only(
        list(OFF_HOURS.run(hourly_frame(usage_profile), pricing, CheckConfig()))
    )
    assert "09:00-18:00 Mon-Fri, UTC+0" in finding.scope
    assert any("--utc-offset" in c for c in finding.caveats)
    assert any("Daylight saving is not modelled" in c for c in finding.caveats)


def test_offset_moves_the_window(pricing, usage_profile):
    """Shifting the local day changes which buckets count as off-hours.

    The fixture's load is built around UTC hours, so a -5 offset drags the
    business window across it and the off-hours share must change.
    """
    frame = hourly_frame(usage_profile)
    at_utc = only(list(OFF_HOURS.run(frame, pricing, CheckConfig())))
    shifted = only(
        list(OFF_HOURS.run(frame, pricing, CheckConfig(utc_offset_hours=Decimal("-5"))))
    )
    assert shifted.measured_usd != at_utc.measured_usd
    assert "UTC-5" in shifted.scope


def test_off_hours_needs_hourly_data(pricing, usage_profile):
    """Daily buckets cannot tell 3am from 3pm, so the check refuses to guess."""
    assert OFF_HOURS.needs_bucket_width == frozenset({"1h"})
    frame, _ = load_usage_csv("tests/fixtures/csv/usage_console.csv", usage_profile)
    unmet = OFF_HOURS.unmet(frame)
    assert len(unmet) == 1
    assert unmet[0].code is GapCode.GRANULARITY_TOO_COARSE
    # The gap names the check that is blocked, in the registry's own words.
    assert "Off-hours and weekend volume (C6b)" in unmet[0].message


def test_a_quiet_share_is_not_reported(pricing, usage_profile):
    """Below the share threshold there is no finding, however many dollars."""
    config = CheckConfig(off_hours_min_share=Decimal("0.95"))
    assert list(OFF_HOURS.run(hourly_frame(usage_profile), pricing, config)) == []


# --------------------------------------------------------------------------
# Through the registry
# --------------------------------------------------------------------------


def test_both_c6_checks_run_on_hourly_data(pricing, usage_profile):
    """Hourly data satisfies both C6 checks' preconditions.

    Asserts on C6 alone, in both directions. Other checks fire on this fixture
    and one (C4) is skipped because it has no context_window column; pinning
    the whole registry's outcome would make this fail every time a check is
    added.
    """
    run = run_checks(hourly_frame(usage_profile), pricing, CheckConfig())
    assert {"C6a", "C6b"} <= {f.check_id for f in run.findings}
    assert not [label for label, _ in run.skipped if "(C6" in label]


def test_daily_data_runs_c6a_and_skips_c6b(pricing, usage_profile):
    frame, _ = load_usage_csv("tests/fixtures/csv/usage_console.csv", usage_profile)
    run = run_checks(frame, pricing, CheckConfig())
    skipped = {label for label, _ in run.skipped}
    assert "Off-hours and weekend volume (C6b)" in skipped
    assert "Day-over-day spend spikes (C6a)" not in skipped
