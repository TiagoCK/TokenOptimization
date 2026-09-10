"""Near-duplicate identifiers, and mixed amount types.

Fixtures 3 and 6 from DATA_FORMATS.md.

The hazard is documented in the cookbook itself: its published sample output
lists `claude-sonnet-4-6` twice and `claude-opus-4-1` twice, despite
accumulating into a dict keyed on the model string. Identical-looking keys that
do not collide means the strings differ in some way the printout does not show,
and the exporter does no normalisation (`result.get("model", "")`).

If one model's spend splits across two rows, every share, ranking and
percentage downstream is computed on a fraction of it.

The response is two-layered, because the two halves need different treatment:

* **Normalise** what is unambiguously the same string -- whitespace and
  zero-width characters. Safe, and invisible to the user.
* **Detect** what is not -- "Claude Opus 5" and "claude-opus-5" are different
  strings a user chose. Merging them would be rewriting their data on an
  inference; reporting the split keeps the numbers checkable.
"""

from decimal import Decimal

from claude_spend_audit.checks import CheckConfig, run_checks
from claude_spend_audit.gaps import GapCode
from claude_spend_audit.ingest import load_usage_csv
from claude_spend_audit.ingest.api_json import load_cost_json
from claude_spend_audit.ingest.schema import normalize_value

FIXTURE = "tests/fixtures/csv/usage_duplicate_models.csv"


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


def test_non_breaking_spaces_are_normalised():
    assert normalize_value("Claude Opus 5") == "Claude Opus 5"


def test_internal_whitespace_is_collapsed():
    assert normalize_value("Claude   Opus  5") == "Claude Opus 5"


def test_case_and_punctuation_are_left_alone():
    """Deliberately conservative. "Claude Opus 5" and "claude-opus-5" are
    different strings the user chose; silently merging them would be rewriting
    their data. They are detected instead."""
    assert normalize_value("Claude Opus 5") == "Claude Opus 5"
    assert normalize_value("CLAUDE-OPUS-5") == "CLAUDE-OPUS-5"


def test_invisible_differences_do_not_split_a_segment(usage_profile):
    """Three of the fixture's four rows differ only invisibly.

    Without normalisation this frame would carry four model strings and split
    one model's spend four ways.
    """
    frame, _ = load_usage_csv(FIXTURE, usage_profile)
    models = set(frame.distinct("model"))
    assert models == {"claude-opus-5", "Claude Opus 5"}
    assert len(frame.usage) == 4  # rows are preserved; only the values merged


# --------------------------------------------------------------------------
# Layer 2 -- detect what cannot safely be merged
# --------------------------------------------------------------------------


def test_an_alias_collision_is_reported(pricing, usage_profile):
    """"Claude Opus 5" and "claude-opus-5" both resolve to one model, so its
    spend is split. Reported, not merged."""
    frame, _ = load_usage_csv(FIXTURE, usage_profile)
    gaps = pricing.alias_collisions(frame)
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.code is GapCode.MODEL_ALIAS_SPLIT
    assert gap.scope == "claude-opus-5"
    assert "'Claude Opus 5'" in gap.message
    assert "'claude-opus-5'" in gap.message
    assert "split across separate rows" in gap.message


def test_the_gap_explains_the_downstream_consequence(pricing, usage_profile):
    frame, _ = load_usage_csv(FIXTURE, usage_profile)
    gap = pricing.alias_collisions(frame)[0]
    assert "every share and ranking" in gap.message
    assert gap.remedy and "whitespace" in gap.remedy


def test_consistent_identifiers_raise_nothing(pricing, usage_profile, usage_csv):
    frame, _ = load_usage_csv(usage_csv, usage_profile)
    assert pricing.alias_collisions(frame) == []


def test_unknown_models_do_not_collide(pricing):
    """Two models absent from the pricing file both resolve to None. They must
    not be reported as aliases of each other."""
    from datetime import datetime, timezone

    from claude_spend_audit.frame import Frame, Segment, TokenBuckets, UsageRow

    t = datetime(2026, 8, 1, tzinfo=timezone.utc)
    frame = Frame(
        usage=(
            UsageRow(start=t, end=t, segment=Segment(model="mystery-a"),
                     tokens=TokenBuckets(output=1)),
            UsageRow(start=t, end=t, segment=Segment(model="mystery-b"),
                     tokens=TokenBuckets(output=1)),
        ),
        grouped_by=frozenset({"model"}),
    )
    assert pricing.alias_collisions(frame) == []


def test_the_split_spend_is_still_priced_correctly(pricing, usage_profile):
    """Both spellings resolve for pricing, so the total is right even while
    the segments are split. It is the per-segment shares that suffer.

        4 rows x (1,000,000 uncached @ $5.00 + 200,000 output @ $25.00)
        = 4 x ($5.00 + $5.00) = $40.00
    """
    frame, _ = load_usage_csv(FIXTURE, usage_profile)
    assert pricing.estimate_frame(frame).usd == Decimal("40.00")


def test_the_gap_reaches_the_report(pricing, usage_profile, capsys):
    from claude_spend_audit.cli import main

    code = main(["audit", FIXTURE])
    out = capsys.readouterr().out
    assert "different spellings" in " ".join(out.split())


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
