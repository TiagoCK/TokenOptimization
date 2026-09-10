from pathlib import Path

import pytest

from claude_spend_audit.ingest.schema import Profile
from claude_spend_audit.pricing import PricingTable

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def pricing() -> PricingTable:
    return PricingTable.load(ROOT / "pricing" / "2026-09.json")


@pytest.fixture(scope="session")
def usage_profile() -> Profile:
    return Profile.load(ROOT / "schemas" / "console_usage_csv.json")


@pytest.fixture(scope="session")
def cost_profile() -> Profile:
    return Profile.load(ROOT / "schemas" / "console_cost_csv.json")


@pytest.fixture(scope="session")
def usage_csv() -> Path:
    return FIXTURES / "csv" / "usage_console.csv"


@pytest.fixture(scope="session")
def cost_csv() -> Path:
    return FIXTURES / "csv" / "cost_console.csv"


@pytest.fixture(scope="session")
def usage_json() -> Path:
    return FIXTURES / "api" / "usage_report.json"
