"""Every rate on file must be traceable, and derived rates must re-derive.

Every dollar figure in the output must trace to a file the user can inspect
and correct.

That only holds if the file itself is internally consistent. A rate marked
'derived: 1.25 x uncached_input' that does not actually equal 1.25x the base is
a number nobody checked, which is exactly what this project cannot afford.
"""

from decimal import Decimal

import pytest

from claude_spend_audit.frame import TOKEN_FIELDS
from claude_spend_audit.pricing import PricingTable

# 'derived: <factor> x <bucket>' -- the only derivation form in use.
DERIVED_PREFIX = "derived:"


def test_every_rate_declares_its_provenance(pricing):
    for group, model_id, bucket, rate, provenance in pricing.iter_rates():
        assert provenance, f"{group}.{model_id}.{bucket} has no provenance"
        assert provenance == "published" or provenance.startswith(DERIVED_PREFIX), (
            f"{group}.{model_id}.{bucket}: provenance {provenance!r} is neither "
            "'published' nor a stated derivation"
        )


def test_derived_rates_actually_re_derive(pricing):
    """Re-run the stated arithmetic and compare. Catches drift in the file."""
    checked = 0
    for group, model_id, bucket, rate, provenance in pricing.iter_rates():
        if not provenance.startswith(DERIVED_PREFIX):
            continue
        # e.g. "derived: 1.25 x uncached_input"
        expression = provenance[len(DERIVED_PREFIX) :].strip()
        factor_text, _, source_bucket = expression.partition(" x ")
        factor = Decimal(factor_text.strip())
        source_bucket = source_bucket.strip()

        assert source_bucket in TOKEN_FIELDS, (
            f"{group}.{model_id}.{bucket}: derivation names unknown bucket "
            f"{source_bucket!r}"
        )
        base = pricing.base_rate(group, model_id, source_bucket)
        assert rate == factor * base, (
            f"{group}.{model_id}.{bucket}: file says ${rate}/MTok but "
            f"{factor} x ${base} = ${factor * base}"
        )
        checked += 1

    # If this drops to zero the test has quietly stopped testing anything.
    assert checked > 0, "no derived rates found -- has the file changed shape?"


def test_derived_cache_rates_use_the_published_multipliers(pricing):
    """The derivation factors must be the published ones, not invented.

    Published multipliers: 5m write 1.25x, 1h write 2x, cache read 0.1x.
    """
    assert pricing.multiplier("cache_write_5m") == Decimal("1.25")
    assert pricing.multiplier("cache_write_1h") == Decimal("2")
    assert pricing.multiplier("cache_read") == Decimal("0.1")


def test_published_standard_rates_match_the_published_multipliers(pricing):
    """Standard-tier cache rates should equal multiplier x base input.

    Both sides are transcribed independently from the pricing page -- the
    per-model table and the multiplier table -- so agreement is a real check on
    the transcription. Fable 5.1 and Mythos 5.1 are excluded: their cache reads
    are 0.025x, not the standard 0.1x, per the table's own footnote.
    """
    exceptions = {"claude-fable-5-1", "claude-mythos-5-1"}
    for model_id in pricing.model_ids:
        base = pricing.base_rate("models", model_id, "uncached_input")
        for bucket in ("cache_write_5m", "cache_write_1h", "cache_read"):
            if bucket == "cache_read" and model_id in exceptions:
                continue
            expected = pricing.multiplier(bucket) * base
            actual = pricing.base_rate("models", model_id, bucket)
            assert actual == expected, (
                f"{model_id}.{bucket}: file says ${actual}/MTok, but "
                f"{pricing.multiplier(bucket)} x ${base} = ${expected}"
            )


def test_fable_cache_read_is_the_documented_exception(pricing):
    """Footnote 1: Fable 5.1 / Mythos 5.1 read at 0.025x, not 0.1x."""
    for model_id in ("claude-fable-5-1", "claude-mythos-5-1"):
        base = pricing.base_rate("models", model_id, "uncached_input")
        read = pricing.base_rate("models", model_id, "cache_read")
        assert read == Decimal("0.025") * base
        assert read == Decimal("0.25")


def test_batch_multiplier_matches_the_published_batch_table(pricing):
    """0.5x should reproduce the published per-model batch rates exactly.

    Spot-checked against the batch pricing table:
        Opus 5     $2.50 in / $12.50 out
        Sonnet 5   $1.00 in /  $5.00 out
        Haiku 4.5  $0.50 in /  $2.50 out
    """
    expected = {
        "claude-opus-5": (Decimal("2.50"), Decimal("12.50")),
        "claude-sonnet-5": (Decimal("1"), Decimal("5")),
        "claude-haiku-4-5": (Decimal("0.50"), Decimal("2.50")),
    }
    for model_id, (batch_in, batch_out) in expected.items():
        rate_in = pricing.rate_for(model_id, "uncached_input", service_tier="batch")
        rate_out = pricing.rate_for(model_id, "output", service_tier="batch")
        assert rate_in.usd_per_mtok == batch_in
        assert rate_out.usd_per_mtok == batch_out


def test_pricing_file_records_where_it_came_from(pricing):
    assert pricing.version == "2026-09"
    assert pricing.fetched_at == "2026-09-09"
    assert "pricing" in pricing.source_url


def test_rates_are_strings_in_the_file_not_json_floats():
    """A JSON float would round; loading must reject one outright."""
    from claude_spend_audit.pricing import PricingError

    table = PricingTable(
        {
            "pricing_version": "test",
            "models": {
                "m": {"rates": {"uncached_input": {"usd_per_mtok": 0.1}}},
            },
        }
    )
    with pytest.raises(PricingError, match="floats"):
        table.rate_for("m", "uncached_input")
