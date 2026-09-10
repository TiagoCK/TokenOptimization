"""Near-duplicate identifiers, and mixed amount types.

Fixtures 3 and 6 from DATA_FORMATS.md.

The hazard is documented in the cookbook itself: its published sample output
lists `claude-sonnet-4-6` twice and `claude-opus-4-1` twice, despite
accumulating into a dict keyed on the model string. Identical-looking keys that
do not collide means the strings differ in some way the printout does not show,
and the exporter does no normalisation (`result.get("model", "")`).

If one model's spend splits across two rows, every share, ranking and
percentage downstream is computed on a fraction of it.

The response is two-layered, because the halves need different treatment:

* **Normalise** what is unambiguously the same string -- whitespace and
  zero-width characters. Safe, and invisible.
* **Merge** what the pricing file can *prove* is one model. "Claude Opus 5"
  and "claude-opus-5" resolve to the same entry, so they are combined under
  the canonical id and every share is computed on the whole. Merging rewrites
  the source data, so it is reported in the caveats naming the strings
  involved -- done openly rather than quietly.

A spelling the pricing file cannot resolve is left exactly as it arrived:
nothing proves it is a duplicate of anything.
"""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from claude_spend_audit.frame import (
    CostRow,
    Frame,
    Segment,
    TokenBuckets,
    UsageRow,
)
from claude_spend_audit.gaps import GapCode
from claude_spend_audit.ingest import load_usage_csv
from claude_spend_audit.ingest.api_json import load_cost_json
from claude_spend_audit.ingest.schema import normalize_value

FIXTURE = "tests/fixtures/csv/usage_duplicate_models.csv"
T = datetime(2026, 8, 1, tzinfo=timezone.utc)


def frame_of(*models: str) -> Frame:
    return Frame(
        usage=tuple(
            UsageRow(
                start=T, end=T, segment=Segment(model=m), tokens=TokenBuckets(output=1)
            )
            for m in models
        ),
        grouped_by=frozenset({"model"}),
    )


# --------------------------------------------------------------------------
# Layer 1 -- normalise
# --------------------------------------------------------------------------


def test_trailing_whitespace_is_trimmed():
    assert normalize_value("claude-opus-5 ") == "claude-opus-5"
    assert normalize_value("  claude-opus-5  ") == "claude-opus-5"


def test_zero_width_characters_are_stripped():
    """The likeliest cause of two identical-looking dict keys."""
    assert normalize_value("claude-opus-5​") == "claude-opus-5"
    assert normalize_value("﻿claude-opus-5") == "claude-opus-5"
    assert normalize_value("claude‍-opus-5") == "claude-opus-5"


def test_non_breaking_spaces_become_spaces_not_nothing():
    """A non-breaking space is a space. Deleting it as if it were zero-width
    turns "Claude<nbsp>Opus<nbsp>5" into "ClaudeOpus5" -- a bug this test
    caught during development."""
    assert normalize_value("Claude Opus 5") == "Claude Opus 5"


def test_internal_whitespace_is_collapsed():
    assert normalize_value("Claude   Opus  5") == "Claude Opus 5"


def test_case_and_punctuation_are_left_alone():
    """Normalisation is deliberately conservative. Case and separators are
    handled by the merge step, which can prove two strings are one model."""
    assert normalize_value("Claude Opus 5") == "Claude Opus 5"
    assert normalize_value("CLAUDE-OPUS-5") == "CLAUDE-OPUS-5"


def test_invisible_differences_do_not_split_a_segment(usage_profile):
    """Three of the fixture's four rows differ only invisibly. Without
    normalisation this frame would carry four model strings."""
    frame, _ = load_usage_csv(FIXTURE, usage_profile)
    assert set(frame.distinct("model")) == {"claude-opus-5", "Claude Opus 5"}
    assert len(frame.usage) == 4


# --------------------------------------------------------------------------
# Layer 2 -- merge what the pricing file can prove is one model
# --------------------------------------------------------------------------


def test_aliases_are_merged_into_the_canonical_id(pricing, usage_profile):
    frame, _ = load_usage_csv(FIXTURE, usage_profile)
    assert len(frame.distinct("model")) == 2

    merged, _ = pricing.merge_model_aliases(frame)
    assert set(merged.distinct("model")) == {"claude-opus-5"}
    assert len(merged.usage) == 4  # rows preserved; only the label rewritten


def test_the_merge_is_reported_not_done_quietly(pricing, usage_profile):
    """It rewrites the source data, so the reader sees which strings were
    treated as the same thing and can disagree if they know better."""
    frame, _ = load_usage_csv(FIXTURE, usage_profile)
    _, gaps = pricing.merge_model_aliases(frame)
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.code is GapCode.MODEL_ALIAS_MERGED
    assert gap.scope == "claude-opus-5"
    assert "'Claude Opus 5'" in gap.message
    assert "'claude-opus-5'" in gap.message
    assert "have been combined" in gap.message
    assert "every share and ranking" in gap.message


def test_cost_rows_are_merged_too(pricing):
    """Merging usage but not cost would put the two halves of the report out
    of step with each other."""
    frame = replace(
        frame_of("Claude Opus 5", "claude-opus-5"),
        cost=(
            CostRow(start=T, end=T, amount_usd=Decimal("1"), model="Claude Opus 5"),
            CostRow(start=T, end=T, amount_usd=Decimal("1"), model="claude-opus-5"),
        ),
    )
    merged, gaps = pricing.merge_model_aliases(frame)
    assert {row.model for row in merged.cost} == {"claude-opus-5"}
    assert len(gaps) == 1


def test_consistent_identifiers_are_left_untouched(pricing, usage_profile, usage_csv):
    """No collision, no rewrite -- and the frame is returned unchanged rather
    than rebuilt."""
    frame, _ = load_usage_csv(usage_csv, usage_profile)
    merged, gaps = pricing.merge_model_aliases(frame)
    assert gaps == []
    assert merged is frame


def test_a_lone_display_name_is_not_rewritten(pricing):
    """Only collisions are merged. One consistent spelling stays as the user
    wrote it, because there is no split to fix."""
    merged, gaps = pricing.merge_model_aliases(frame_of("Claude Opus 5"))
    assert gaps == []
    assert set(merged.distinct("model")) == {"Claude Opus 5"}


def test_unknown_models_are_not_merged(pricing):
    """Two models absent from the pricing file both resolve to None. Nothing
    proves they are the same thing, so neither is touched."""
    merged, gaps = pricing.merge_model_aliases(frame_of("mystery-a", "mystery-b"))
    assert gaps == []
    assert set(merged.distinct("model")) == {"mystery-a", "mystery-b"}


def test_a_dated_snapshot_merges_with_its_base_id(pricing):
    """The alias table maps dated snapshots onto canonical ids, so those
    collide too."""
    merged, gaps = pricing.merge_model_aliases(
        frame_of("claude-haiku-4-5", "claude-haiku-4-5-20251001")
    )
    assert set(merged.distinct("model")) == {"claude-haiku-4-5"}
    assert len(gaps) == 1


def test_merging_does_not_change_the_total(pricing, usage_profile):
    """Both spellings already resolved for pricing, so the money was always
    right. Merging fixes the shares, not the total.

        4 rows x (1,000,000 uncached @ $5.00 + 200,000 output @ $25.00)
        = 4 x ($5.00 + $5.00) = $40.00
    """
    frame, _ = load_usage_csv(FIXTURE, usage_profile)
    merged, _ = pricing.merge_model_aliases(frame)
    assert pricing.estimate_frame(frame).usd == Decimal("40.00")
    assert pricing.estimate_frame(merged).usd == Decimal("40.00")


def test_the_merge_reaches_the_report(capsys):
    from claude_spend_audit.cli import main

    main(["audit", FIXTURE])
    out = " ".join(capsys.readouterr().out.split())
    assert "have been combined under the canonical id" in out


def test_the_merged_model_is_what_inspect_lists(capsys):
    """inspect prints the dimension values, so it shows the merge took effect
    rather than only that it was announced."""
    from claude_spend_audit.cli import main

    main(["inspect", FIXTURE])
    out = " ".join(capsys.readouterr().out.split())
    assert "model 1 distinct claude-opus-5" in out
    assert "Claude Opus 5" not in out.split("DIMENSIONS PRESENT")[1].split("WHAT")[0]


# --------------------------------------------------------------------------
# Fixture 6 -- amount as a string in one row, a number in another
# --------------------------------------------------------------------------


def test_string_and_numeric_amounts_parse_identically():
    """The cookbook handles this with float(); this tool uses Decimal.

    Both rows are 12345 minor units = $123.45, so the total is $246.90.
    """
    frame = load_cost_json("tests/fixtures/api/cost_report_mixed_amounts.json")
    assert len(frame.cost) == 2
    assert frame.cost[0].amount_usd == Decimal("123.45")
    assert frame.cost[1].amount_usd == Decimal("123.45")
    assert frame.billed_total_usd == Decimal("246.90")


def test_both_amount_types_stay_decimal():
    """A float would round; the type is the guarantee, not the value."""
    frame = load_cost_json("tests/fixtures/api/cost_report_mixed_amounts.json")
    for row in frame.cost:
        assert isinstance(row.amount_usd, Decimal)


def test_a_fractional_minor_unit_amount_keeps_its_precision():
    """The API's own example is "123.78912" -- minor units are not integers."""
    frame = load_cost_json(
        {
            "data": [
                {
                    "starting_at": "2026-08-01T00:00:00Z",
                    "ending_at": "2026-08-02T00:00:00Z",
                    "results": [{"amount": "123.78912", "currency": "USD"}],
                }
            ]
        }
    )
    assert frame.cost[0].amount_usd == Decimal("1.2378912")
