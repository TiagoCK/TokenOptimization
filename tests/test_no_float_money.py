"""No float may touch a money path.

The tool prints dollar figures that someone may take to their CFO
(PROJECT_BRIEF.md §7). Binary floating point cannot represent $0.10, and errors
compound across a month of buckets. Decimal throughout, enforced two ways: by
inspecting the values that come out, and by grepping the source for the
constructs that would introduce a float in the first place.
"""

import ast
import re
from decimal import Decimal
from pathlib import Path

import pytest

from claude_spend_audit.ingest import load_cost_csv, load_usage_csv

SRC = Path(__file__).resolve().parents[1] / "src" / "claude_spend_audit"

MONEY_NAMES = {
    "amount_usd",
    "usd",
    "usd_per_mtok",
    "usd_per_1000_requests",
    "usd_per_container_hour",
    "usd_per_session_hour",
}


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------


def test_cost_rows_carry_decimals(cost_csv, cost_profile):
    frame, _ = load_cost_csv(cost_csv, cost_profile)
    assert frame.cost
    for row in frame.cost:
        assert isinstance(row.amount_usd, Decimal)


def test_billed_total_is_decimal(cost_csv, cost_profile):
    frame, _ = load_cost_csv(cost_csv, cost_profile)
    assert isinstance(frame.billed_total_usd, Decimal)


def test_rates_are_decimal(pricing):
    rate = pricing.rate_for("claude-opus-5", "output")
    assert isinstance(rate.usd_per_mtok, Decimal)
    assert isinstance(rate.cost_of(1_000_000), Decimal)


def test_estimates_are_decimal(pricing, usage_csv, usage_profile):
    frame, _ = load_usage_csv(usage_csv, usage_profile)
    assert isinstance(pricing.estimate_frame(frame).usd, Decimal)


def test_decimal_arithmetic_is_exact_where_float_would_not_be(pricing):
    """0.1 + 0.2 != 0.3 in binary floating point. Here it must."""
    ten_cents = pricing.rate_for("claude-haiku-4-5", "cache_read").cost_of(1_000_000)
    assert ten_cents == Decimal("0.10")
    assert ten_cents * 3 == Decimal("0.30")
    assert float(ten_cents) * 3 != 0.30  # the bug this test exists to prevent


def test_json_output_serialises_money_as_strings(usage_csv, usage_profile, capsys):
    """A JSON float would re-introduce the rounding at the boundary."""
    import json

    from claude_spend_audit.cli import main

    main(["inspect", str(usage_csv), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload["spend"]["estimated_usd"], str)


# --------------------------------------------------------------------------
# Source
# --------------------------------------------------------------------------


def _python_files():
    return sorted(SRC.rglob("*.py"))


def test_no_float_calls_in_money_code():
    """float(...) anywhere in the package is almost certainly a money bug."""
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "float"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"float() called in: {', '.join(offenders)}"


def test_money_fields_are_annotated_decimal():
    """Every field whose name says dollars must be typed as one."""
    bad = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AnnAssign) or not isinstance(
                node.target, ast.Name
            ):
                continue
            if node.target.id not in MONEY_NAMES:
                continue
            annotation = ast.unparse(node.annotation)
            if "Decimal" not in annotation:
                bad.append(f"{path.name}:{node.lineno} {node.target.id}: {annotation}")
    assert not bad, f"money fields not typed Decimal: {bad}"


def test_no_float_literals_assigned_to_money_names():
    pattern = re.compile(
        r"\b(" + "|".join(MONEY_NAMES) + r")\s*=\s*-?\d+\.\d+(?![\d\"'])"
    )
    offenders = []
    for path in _python_files():
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pattern.search(line):
                offenders.append(f"{path.name}:{lineno}")
    assert not offenders, f"float literal assigned to a money name: {offenders}"


def test_pricing_json_stores_rates_as_strings():
    """A bare JSON number would be parsed as a float by json.loads."""
    import json

    path = Path(__file__).resolve().parents[1] / "pricing" / "2026-09.json"
    data = json.loads(path.read_text(encoding="utf-8"))

    def walk(node, trail=""):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in MONEY_NAMES or key == "value":
                    assert isinstance(value, str), (
                        f"{trail}.{key} is {type(value).__name__} {value!r}; "
                        "rates must be strings so Decimal can parse them exactly"
                    )
                walk(value, f"{trail}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{trail}[{i}]")

    walk(data)


def test_pricing_loader_rejects_a_float_rate():
    from claude_spend_audit.pricing import PricingError, PricingTable

    table = PricingTable(
        {"models": {"m": {"rates": {"output": {"usd_per_mtok": 25.0}}}}}
    )
    with pytest.raises(PricingError, match="floats"):
        table.rate_for("m", "output")
